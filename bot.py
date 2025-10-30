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
import re  # Added for email validation

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
    raise ValueError("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON no configurado")  # Changed to raise error early

# Prompt mejorado para mejor extracción
PROMPT_BASE = """
Eres el asistente de Eqilibrio.cl (Quiropraxia en Viña del Mar). Sé CONVERSACIONAL y cercano.

**Servicios:** Primera consulta $35k | Sesión $40k | Pack 4 $120k
**Horarios:** Mar/Jue 15-19h | Mié/Vie 10-18h | Sáb 10-13h
**Ubicación:** Av. Reñaca Norte 25, Of. 1506, Viña del Mar

**AGENDAMIENTO - FLUJO FLEXIBLE:**

Si el usuario dice "en la tarde", "por la mañana", o un día sin hora específica:
{"intent": "show_slots", "date": "YYYY-MM-DD"}

Para agendar necesitas OBLIGATORIO:
- Nombre completo (nombre y apellido)
- Contacto: teléfono (mínimo 8 dígitos) O email
- Fecha: YYYY-MM-DD
- Hora: HH:MM exacta

Si tiene TODO completo y válido:
{"intent": "schedule", "name": "Nombre Apellido", "contact": "teléfono_o_email", "date": "YYYY-MM-DD", "time": "HH:MM"}

Si falta algo:
{"intent": "schedule", "missing": ["los_que_faltan"]}

Otras consultas: Responde en 2-3 líneas.

**SÉ FLEXIBLE:**
- Acepta "Jose" pero pide apellido
- Acepta formatos de fecha variados (conviértelos a YYYY-MM-DD)
- Si dicen "mañana tarde", usa show_slots para mostrar horarios
- Valida que teléfono tenga al menos 8 dígitos o que sea un email válido

**ESCALAMIENTO A HUMANO:**
Si el usuario pregunta sobre:
- Diagnósticos específicos o condiciones médicas complejas
- Casos que requieren evaluación profesional
- Dudas que no puedes responder con seguridad
- Solicita hablar con el quiropráctico directamente

Responde con: {"intent": "human_support", "reason": "breve razón"}

Ejemplos de cuándo escalar:
- "Tengo una hernia discal, ¿me pueden atender?"
- "Estoy embarazada, ¿puedo recibir tratamiento?"
- "Tuve una cirugía hace 1 mes"
- "Tomo anticoagulantes"
- Cualquier condición médica seria
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

def needs_human_intervention(message):
    """Detecta si el mensaje requiere intervención humana"""
    critical_keywords = [
        'hernia', 'embarazada', 'embarazo', 'cirugía', 'operado', 'operada',
        'anticoagulante', 'marcapasos', 'cáncer', 'tumor', 'fractura',
        'infarto', 'derrame', 'diabetes severa', 'epilepsia'
    ]
    
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in critical_keywords)

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
        # Detección automática de casos críticos
        if needs_human_intervention(full_context):
            support_phone = os.getenv('WHATSAPP_SUPPORT', '+56912345678')
            support_name = os.getenv('SUPPORT_NAME', 'nuestro quiropráctico')
            
            response_text = f"Gracias por compartir. Por la naturaleza de tu consulta, es mejor que hables directamente con {support_name} para evaluarte bien:\n\n📱 {support_phone}\n\n¿O prefieres agendar una evaluación?"
            
            send_whatsapp_message(phone_number, response_text)
            return  # No llama a Gemini, responde directo
    
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
                
                if data.get('intent') == 'human_support':
                    reason = data.get('reason', 'consulta específica')
                    support_phone = os.getenv('WHATSAPP_SUPPORT', '+56912345678')
                    support_name = os.getenv('SUPPORT_NAME', 'nuestro quiropráctico')
                    
                    return f"Entiendo que tienes una {reason}. Para darte la mejor orientación, te conectaré con {support_name}:\n\n📱 {support_phone}\n\n¿Prefieres hablar con él directamente? Déjame tu número."
                
                elif data.get('intent') == 'show_slots':
                    date_str = data.get('date')
                    slots = get_available_slots(date_str)
                    if slots:
                        return f"Horarios disponibles el {date_str}: {', '.join(slots)} 😊 ¿Cuál te acomoda?"
                    else:
                        return f"No hay horarios disponibles el {date_str}. ¿Otro día?"
                
                elif data.get('intent') == 'schedule':
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

def get_available_slots(date_str):
    """Obtiene horarios disponibles para una fecha"""
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        dt = TZ.localize(dt)
        weekday = dt.weekday()
        
        # Define rangos según el día
        if weekday == 0 or weekday == 6:  # Lun/Dom
            return None  # Cerrado
        elif weekday in [1, 3]:  # Mar/Jue
            slots = [(15, 0), (16, 0), (17, 0), (18, 0)]
        elif weekday in [2, 4]:  # Mié/Vie
            slots = [(10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0)]
        elif weekday == 5:  # Sáb
            slots = [(10, 0), (11, 0), (12, 0)]
        
        # Verifica disponibilidad de cada slot
        available = []
        for hour, minute in slots:
            slot_dt = dt.replace(hour=hour, minute=minute)
            end_dt = slot_dt + datetime.timedelta(hours=1)
            
            # Solo si es futuro y está libre
            if slot_dt > datetime.datetime.now(TZ) and not check_freebusy(slot_dt, end_dt):
                available.append(f"{hour:02d}:{minute:02d}")
        
        return available
    except:
        return None

def handle_appointment_booking(data):
    try:
        name = data.get('name')
        contact = data.get('contact')
        date_str = data.get('date') 
        time_str = data.get('time')
        
        # Validar nombre completo (al menos 2 palabras)
        if len(name.split()) < 2:
            return "Por favor, dame tu nombre y apellido completo 😊"
        
        # Validar contacto (teléfono de 8+ dígitos O email)
        contact_clean = contact.replace('+', '').replace(' ', '').replace('-', '')
        is_phone = contact_clean.isdigit() and len(contact_clean) >= 8
        is_email = re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', contact) is not None
        
        if not (is_phone or is_email):
            return "Necesito un teléfono válido (8+ dígitos) o un email 📱"
        
        print(f"Intentando agendar: {name} | {contact} | {date_str} | {time_str}")
        # Normaliza formato de hora
        time_str = time_str.replace('.', ':').replace(' ', '')
        if ':' not in time_str and len(time_str) <= 2:
            time_str = f"{time_str}:00"  # "18" → "18:00"

        # Normaliza formato de fecha  
        date_str = date_str.replace('/', '-')
        if date_str.count('-') == 2:
            parts = date_str.split('-')
            if len(parts[0]) == 2:  # DD-MM-YYYY
                date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
        
        # Parsea fecha y hora
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return "Error en fecha/hora. Usa: YYYY-MM-DD y HH:MM (ej: 2025-11-15 15:00)"
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
        model = genai.GenerativeModel('gemini-2.5-pro') 
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error Gemini: {str(e)}")
        return "Disculpa, problemas técnicos. Intenta de nuevo."

def check_freebusy(start_dt, end_dt):
    """Verifica disponibilidad en calendario"""
    if not credentials:
        raise Exception("Credenciales no configuradas")
    
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