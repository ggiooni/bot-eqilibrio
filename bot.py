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

# Prompt mejorado para mejor extracción
PROMPT_BASE = """
Eres el asistente de Eqilibrio.cl (Quiropraxia en Viña del Mar). Sé BREVE (máximo 2-3 líneas).

**Servicios:** Primera consulta $35k | Sesión $40k | Pack 4 $120k
**Horarios:** Mar/Jue 15-19h | Mié/Vie 10-18h | Sáb 10-13h
**Ubicación:** Av. Reñaca Norte 25, Of. 1506, Viña del Mar

**AGENDAMIENTO - MUY IMPORTANTE:**
Analiza TODO el contexto de la conversación para extraer los datos.

Si el usuario quiere agendar, extrae de TODA la conversación:
- Nombre: Busca cualquier nombre mencionado (ej: "Jose Miguel", "soy María")
- Contacto: Teléfono (ej: 896171907, +56912345) o email
- Fecha: Cualquier formato (30-10-2025, hoy, mañana) → Convierte a YYYY-MM-DD
- Hora: Cualquier formato (18:30, 18.30, seis y media) → Convierte a HH:MM (24h)

IMPORTANTE HORAS:
- "18:30", "18.30", "6:30 pm" → "18:30"
- Si solo dice "6" o "18" → "18:00"

SOLO si tienes los 4 datos completos y válidos:
{"intent": "schedule", "name": "nombre_completo", "contact": "teléfono_o_email", "date": "YYYY-MM-DD", "time": "HH:MM"}

Si falta ALGÚN dato o no está claro:
{"intent": "schedule", "missing": ["los_que_faltan"]}

Otras preguntas: Responde brevemente sin mencionar JSON.
"""

# Buffer de mensajes con historial
MESSAGE_BUFFER = defaultdict(lambda: {
    'messages': [],
    'history': [],  # Mantiene historial completo
    'timer': None,
    'lock': threading.Lock(),
    'last_activity': time.time()
})
BUFFER_DELAY = 5  # 5 segundos para dar tiempo a escribir
SESSION_CLEANUP_TIME = 600  # 10 minutos

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
    """Procesa mensajes agrupados con historial"""
    session = MESSAGE_BUFFER[phone_number]
    
    with session['lock']:
        if not session['messages']:
            return
        
        # Agrupa nuevos mensajes
        grouped_message = '\n'.join(session['messages'])
        
        # Agrega al historial
        session['history'].append(grouped_message)
        
        # Mantiene solo últimos 10 intercambios
        if len(session['history']) > 10:
            session['history'] = session['history'][-10:]
        
        # Crea contexto completo
        full_context = '\n---\n'.join(session['history'])
        
        session['messages'] = []
        session['timer'] = None
    
    try:
        # Incluye TODO el historial en el prompt
        today = datetime.datetime.now(TZ)
        current_date_info = f"""
        FECHA ACTUAL: {today.strftime('%Y-%m-%d')} (es {today.strftime('%A %d de %B')})
        Hora actual: {today.strftime('%H:%M')}

        Si el usuario dice:
        - "hoy" = {today.strftime('%Y-%m-%d')}
        - "mañana" = {(today + datetime.timedelta(days=1)).strftime('%Y-%m-%d')}
        """

        ai_prompt = f"{PROMPT_BASE}\n\n{current_date_info}\n\nHistorial:\n{full_context}"
        ai_response = generate_ai_response(ai_prompt)
        
        # Procesa y envía respuesta
        response_text = process_ai_response(ai_response)
        
        # Guarda respuesta en historial
        with session['lock']:
            session['history'].append(f"[Bot]: {response_text}")
        
        send_whatsapp_message(phone_number, response_text)
        
    except Exception as e:
        print(f"Error procesando: {str(e)}")
        send_whatsapp_message(
            phone_number,
            "Disculpa, hubo un error. ¿Puedes repetir?"
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
        # Limita mensaje a 1500 caracteres
        if len(message) > 1500:
            message = message[:1497] + "..."
        
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=message,
            from_=from_whatsapp,
            to=to_number
        )
        print(f"✓ Mensaje enviado ({len(message)} chars)")
    except Exception as e:
        print(f"✗ Error enviando: {str(e)}")

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
                            'name': 'nombre completo',
                            'contact': 'teléfono o email',
                            'date': 'fecha (formato: 2025-11-15)',
                            'time': 'hora (formato: 15:00)'
                        }
                        missing_texts = [missing_map.get(f, f) for f in data['missing']]
                        return f"Me falta: {', '.join(missing_texts)} 📅"
                    else:
                        return handle_appointment_booking(data)
            except json.JSONDecodeError:
                pass
        
        # Si no es JSON válido, retorna el texto limpio
        if '{"intent"' in cleaned:
            cleaned = cleaned.split('{"intent"')[0].strip()
        
        return cleaned if cleaned else "¿En qué más puedo ayudarte?"
        
    except Exception as e:
        print(f"Error procesando: {str(e)}")
        return "¿Cómo puedo ayudarte?"

def handle_appointment_booking(data):
    """Maneja agendamiento de cita"""
    try:
        name = data.get('name')
        contact = data.get('contact')
        date_str = data.get('date')
        time_str = data.get('time')
        
        print(f"Intentando agendar: {name} | {contact} | {date_str} | {time_str}")
        
        # Parsea fecha y hora
        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt = TZ.localize(dt)
        end_dt = dt + datetime.timedelta(hours=1)
        
        # Valida horarios
        error = validate_business_hours(dt)
        if error:
            return error
        
        # Verifica disponibilidad
        if check_freebusy(dt, end_dt):
            return f"❌ {date_str} a las {time_str} ya está ocupado.\n¿Otro horario?"
        
        # Crea cita
        create_appointment(name, contact, dt)
        
        # Formato de respuesta
        fecha_formato = dt.strftime("%d/%m/%Y")
        return f"✅ ¡Listo {name}!\n📅 {fecha_formato} a las {time_str}\n📍 Av. Reñaca Norte 25, Of. 1506\n\n¡Te esperamos!"
        
    except ValueError as e:
        print(f"Error formato: {str(e)}")
        return "Error en fecha/hora. Usa: YYYY-MM-DD y HH:MM (ej: 2025-11-15 15:00)"
    except Exception as e:
        print(f"Error agendando: {str(e)}")
        return "Error al agendar. Llámanos: +56 9 XXXX XXXX"

def validate_business_hours(dt):
    """Valida horarios de negocio"""
    weekday = dt.weekday()
    hour = dt.hour
    minute = dt.minute
    
    # Verifica que no sea pasado
    now = datetime.datetime.now(TZ)
    if dt < now:
        return "❌ Esa fecha/hora ya pasó"
    
    if weekday == 0:
        return "❌ Cerrados los lunes"
    elif weekday == 6:
        return "❌ Cerrados los domingos"
    elif weekday in [1, 3]:  # Mar, Jue
        if not (15 <= hour < 19):
            return "❌ Mar/Jue atendemos 15:00-19:00"
    elif weekday in [2, 4]:  # Mie, Vie
        if not (10 <= hour < 18):
            return "❌ Mié/Vie atendemos 10:00-18:00"
    elif weekday == 5:  # Sab
        if not (10 <= hour < 13):
            return "❌ Sábados 10:00-13:00"
    
    return None

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Webhook de Twilio"""
    incoming_msg = request.values.get('Body', '').strip()
    from_phone = request.values.get('From', '')
    
    if not incoming_msg or not from_phone:
        return '', 200
    
    print(f"→ Mensaje de {from_phone}: {incoming_msg}")
    
    cleanup_old_sessions()
    
    session = MESSAGE_BUFFER[from_phone]
    
    with session['lock']:
        session['messages'].append(incoming_msg)
        session['last_activity'] = time.time()
        
        # Cancela timer anterior
        if session['timer']:
            session['timer'].cancel()
        
        # Crea nuevo timer
        session['timer'] = threading.Timer(
            BUFFER_DELAY,
            process_buffered_messages,
            args=[from_phone]
        )
        session['timer'].start()
        
        print(f"⏱️  Timer iniciado ({BUFFER_DELAY}s) - Mensajes en buffer: {len(session['messages'])}")
    
    return '', 200

def generate_ai_response(prompt):
    """Genera respuesta con Gemini"""
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error Gemini 2.0: {str(e)}")
        # Fallback
        try:
            model = genai.GenerativeModel('gemini-1.5-flash-latest')
            response = model.generate_content(prompt)
            return response.text
        except Exception as e2:
            print(f"Error Gemini 1.5: {str(e2)}")
            return "Disculpa, problemas técnicos. Intenta de nuevo."

def check_freebusy(start_dt, end_dt):
    """Verifica disponibilidad en calendario"""
    if not credentials:
        print("⚠️  Sin credenciales Calendar")
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
        print(f"📅 Disponibilidad: {'Ocupado' if busy else 'Libre'}")
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
        
        result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✓ Cita creada: {name} - {dt.strftime('%Y-%m-%d %H:%M')}")
        print(f"  ID: {result.get('id')}")
        
    except Exception as e:
        print(f"✗ Error creando cita: {str(e)}")
        raise

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return {
        'status': 'ok', 
        'service': 'equilibrio-bot',
        'timestamp': datetime.datetime.now(TZ).isoformat()
    }, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"🚀 Equilibrio Bot iniciando en puerto {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)