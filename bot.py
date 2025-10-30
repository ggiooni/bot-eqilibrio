from flask import Flask, request
from twilio.rest import Client
import os
from dotenv import load_dotenv
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import datetime
import pytz
import json
import time
import threading
from collections import defaultdict

load_dotenv()

app = Flask(__name__)

# Configuración Gemini
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

# Configuración Google Calendar
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.getenv('CALENDAR_ID', '059bad589de3d4b2457841451a3939ba605411559b7728fc617765e69947b3e5@group.calendar.google.com')
TZ = pytz.timezone('America/Santiago')

# Cargar credenciales
credentials_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
if credentials_json:
    credentials_dict = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict, scopes=SCOPES
    )
else:
    print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON no configurado")
    credentials = None

# Prompt base - MÁS CORTO para evitar respuestas largas
PROMPT_BASE = """
Eres el asistente de Eqilibrio.cl (Quiropraxia, Kinesiología, Medicina China en Viña del Mar). 
Sé breve, cálido y profesional. MÁXIMO 3-4 líneas por respuesta.

**Servicios y Precios:**
- Primera consulta: $35.000
- Sesión normal: $40.000  
- Pack 4 sesiones: $120.000
- Método Equilibrio: Quiropraxia + kinesiología + acupuntura en una sesión

**Horarios:**
Mar/Jue: 15-19h | Mié/Vie: 10-18h | Sáb: 10-13h | Lun/Dom: CERRADO

**Ubicación:** Av. Reñaca Norte 25, Of. 1506, Viña del Mar

**AGENDAMIENTO - IMPORTANTE:**
Si quiere agendar, necesitas: nombre, contacto, fecha (YYYY-MM-DD), hora (HH:MM).

- Si tiene TODO, responde SOLO: {"intent": "schedule", "name": "X", "contact": "Y", "date": "Z", "time": "W"}
- Si falta algo, responde SOLO: {"intent": "schedule", "missing": ["lo que falta"]}
- Otras consultas: Responde en 2-3 líneas máximo, sin repetir info innecesaria.

NUNCA repitas el JSON en texto. NUNCA des respuestas largas.
"""

# Buffer de mensajes
MESSAGE_BUFFER = defaultdict(lambda: {
    'messages': [],
    'timer': None,
    'lock': threading.Lock(),
    'last_activity': time.time()
})
BUFFER_DELAY = 4  # 4 segundos para agrupar
SESSION_CLEANUP_TIME = 300

def cleanup_old_sessions():
    """Limpia sesiones antiguas"""
    current_time = time.time()
    to_delete = [
        phone for phone, data in MESSAGE_BUFFER.items()
        if data.get('last_activity', 0) < current_time - SESSION_CLEANUP_TIME
    ]
    for phone in to_delete:
        del MESSAGE_BUFFER[phone]

def process_buffered_messages(phone_number):
    """Procesa mensajes agrupados"""
    session = MESSAGE_BUFFER[phone_number]
    
    with session['lock']:
        if not session['messages']:
            return
        
        grouped_message = '\n'.join(session['messages'])
        session['messages'] = []
        session['timer'] = None
    
    try:
        ai_prompt = f"{PROMPT_BASE}\n\nConsulta del usuario:\n{grouped_message}"
        ai_response = generate_ai_response(ai_prompt)
        
        # Procesa y envía respuesta
        response_text = process_ai_response(ai_response)
        send_whatsapp_message(phone_number, response_text)
        
    except Exception as e:
        print(f"Error procesando: {str(e)}")
        send_whatsapp_message(
            phone_number,
            "Disculpa, hubo un error. ¿Puedes repetir tu consulta?"
        )

def send_whatsapp_message(to_number, message):
    """Envía mensaje vía Twilio"""
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    from_whatsapp = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
    
    if not account_sid or not auth_token:
        print("Error: Credenciales Twilio no configuradas")
        return
    
    try:
        # Limita mensaje a 1500 caracteres (Twilio limit: 1600)
        if len(message) > 1500:
            message = message[:1497] + "..."
        
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=message,
            from_=from_whatsapp,
            to=to_number
        )
        print(f"Mensaje enviado a {to_number}: {len(message)} chars")
    except Exception as e:
        print(f"Error enviando mensaje: {str(e)}")

def process_ai_response(ai_response):
    """Procesa respuesta: JSON o texto"""
    try:
        # Limpia formato markdown
        cleaned = ai_response.strip()
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        elif cleaned.startswith('```'):
            cleaned = cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        
        # Busca JSON en la respuesta
        if '{' in cleaned and '}' in cleaned:
            start = cleaned.find('{')
            end = cleaned.rfind('}') + 1
            json_str = cleaned[start:end]
            
            try:
                data = json.loads(json_str)
                
                if data.get('intent') == 'schedule':
                    if 'missing' in data:
                        missing_map = {
                            'name': 'nombre',
                            'contact': 'teléfono/email',
                            'date': 'fecha (ej: 2025-11-15)',
                            'time': 'hora (ej: 15:00)'
                        }
                        missing_texts = [missing_map.get(f, f) for f in data['missing']]
                        return f"Para agendar necesito: {', '.join(missing_texts)} 📅"
                    else:
                        return handle_appointment_booking(data)
            except json.JSONDecodeError:
                pass
        
        # Si no es JSON válido, retorna el texto limpio
        # Elimina el JSON si apareció como texto
        if '{"intent"' in cleaned:
            cleaned = cleaned.split('{"intent"')[0].strip()
        
        return cleaned if cleaned else "¿En qué puedo ayudarte?"
        
    except Exception as e:
        print(f"Error procesando respuesta: {str(e)}")
        return "¿En qué más puedo ayudarte?"

def handle_appointment_booking(data):
    """Maneja agendamiento de cita"""
    try:
        name = data.get('name')
        contact = data.get('contact')
        date_str = data.get('date')
        time_str = data.get('time')
        
        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt = TZ.localize(dt)
        end_dt = dt + datetime.timedelta(hours=1)
        
        # Valida horarios
        error = validate_business_hours(dt)
        if error:
            return error
        
        # Verifica disponibilidad
        if check_freebusy(dt, end_dt):
            return f"Esa hora está ocupada. ¿Prefieres otro horario? 📅"
        
        # Crea cita
        create_appointment(name, contact, dt)
        return f"✅ Cita agendada para {name}\n📅 {date_str} a las {time_str}\n\n¡Te esperamos!"
        
    except ValueError:
        return "Formato incorrecto. Usa YYYY-MM-DD para fecha y HH:MM para hora."
    except Exception as e:
        print(f"Error agendando: {str(e)}")
        return "Error al agendar. Intenta de nuevo o llámanos."

def validate_business_hours(dt):
    """Valida horarios de negocio"""
    weekday = dt.weekday()
    hour = dt.hour
    
    if weekday == 0:
        return "❌ Cerrados los lunes"
    elif weekday == 6:
        return "❌ Cerrados los domingos"
    elif weekday in [1, 3]:  # Mar, Jue
        if not (15 <= hour < 19):
            return "Mar/Jue: 15:00-19:00"
    elif weekday in [2, 4]:  # Mie, Vie
        if not (10 <= hour < 18):
            return "Mié/Vie: 10:00-18:00"
    elif weekday == 5:  # Sab
        if not (10 <= hour < 13):
            return "Sábados: 10:00-13:00"
    
    return None

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Webhook de Twilio"""
    incoming_msg = request.values.get('Body', '').strip()
    from_phone = request.values.get('From', '')
    
    if not incoming_msg or not from_phone:
        return '', 200
    
    cleanup_old_sessions()
    
    session = MESSAGE_BUFFER[from_phone]
    
    with session['lock']:
        session['messages'].append(incoming_msg)
        session['last_activity'] = time.time()
        
        if session['timer']:
            session['timer'].cancel()
        
        session['timer'] = threading.Timer(
            BUFFER_DELAY,
            process_buffered_messages,
            args=[from_phone]
        )
        session['timer'].start()
    
    return '', 200

def generate_ai_response(prompt):
    """Genera respuesta con Gemini"""
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error Gemini: {str(e)}")
        # Fallback a modelo estable
        try:
            model = genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content(prompt)
            return response.text
        except Exception as e2:
            print(f"Error Gemini fallback: {str(e2)}")
            return "Disculpa, tengo problemas técnicos. ¿Puedes repetir?"

def check_freebusy(start_dt, end_dt):
    """Verifica disponibilidad en calendario"""
    if not credentials:
        return False
    
    try:
        service = build('calendar', 'v3', credentials=credentials)
        body = {
            "timeMin": start_dt.isoformat(),
            "timeMax": end_dt.isoformat(),
            "items": [{"id": CALENDAR_ID}]
        }
        response = service.freebusy().query(body=body).execute()
        busy = response['calendars'][CALENDAR_ID].get('busy', [])
        return len(busy) > 0
    except Exception as e:
        print(f"Error calendario: {str(e)}")
        return False

def create_appointment(name, contact, dt):
    """Crea evento en Google Calendar"""
    if not credentials:
        raise Exception("Credenciales no configuradas")
    
    try:
        service = build('calendar', 'v3', credentials=credentials)
        end_dt = dt + datetime.timedelta(hours=1)
        
        event = {
            'summary': f'Cita: {name}',
            'description': f'Contacto: {contact}\nMétodo Equilibrio',
            'start': {
                'dateTime': dt.isoformat(),
                'timeZone': 'America/Santiago'
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'America/Santiago'
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},
                    {'method': 'popup', 'minutes': 60}
                ]
            }
        }
        
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✓ Cita creada: {name} - {dt}")
        
    except Exception as e:
        print(f"Error creando cita: {str(e)}")
        raise

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return {'status': 'ok', 'service': 'equilibrio-bot'}, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)