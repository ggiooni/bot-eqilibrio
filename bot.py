from flask import Flask, request
from twilio.rest import Client
import os
from dotenv import load_dotenv
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import pytz
import json
import time
import threading
from collections import defaultdict
import re
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager

load_dotenv()

app = Flask(__name__)

# ============================================
# CONFIGURACIÓN DE LOGGING
# ============================================
os.makedirs('logs', exist_ok=True)

# Logger general
logger = logging.getLogger('equilibrio_bot')
logger.setLevel(logging.INFO)

# Handler para archivo con rotación
file_handler = RotatingFileHandler(
    'logs/bot.log', 
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))

# Handler para consola
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
))

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Logger específico para conversaciones
conversation_logger = logging.getLogger('conversations')
conversation_logger.setLevel(logging.INFO)
conv_handler = RotatingFileHandler(
    'logs/conversations.log',
    maxBytes=10*1024*1024,
    backupCount=10
)
conv_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(message)s'
))
conversation_logger.addHandler(conv_handler)

# ============================================
# CONFIGURACIÓN BASE
# ============================================
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
# Twilio
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.getenv('CALENDAR_ID', '059bad589de3d4b2457841451a3939ba605411559b7728fc617765e69947b3e5@group.calendar.google.com')
TZ = pytz.timezone('America/Santiago')

credentials_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
if credentials_json:
    credentials_dict = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict, scopes=SCOPES
    )
else:
    raise ValueError("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON no configurado")

# ============================================
# GESTIÓN DE BASE DE DATOS (SQLite)
# ============================================
DB_PATH = os.getenv('DB_PATH', 'equilibrio_bot.db')

def init_db():
    """Inicializa la base de datos con las tablas necesarias"""
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                phone_number TEXT PRIMARY KEY,
                state TEXT DEFAULT 'ACTIVE',
                context TEXT,
                last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT,
                direction TEXT,
                content TEXT,
                intent TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT,
                name TEXT,
                contact TEXT,
                date_time TIMESTAMP,
                status TEXT DEFAULT 'PENDING',
                google_event_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE TABLE IF NOT EXISTS pending_confirmations (
                phone_number TEXT PRIMARY KEY,
                appointment_data TEXT,
                expires_at TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone_number);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_appointments_datetime ON appointments(date_time);
        ''')
    logger.info("Base de datos inicializada correctamente")
    
@contextmanager
def get_db():
    """Context manager para conexión a BD"""
    # Asegura que la BD existe antes de conectar
    if not os.path.exists(DB_PATH):
        logger.warning(f"⚠️ BD no existe, creando nueva en {DB_PATH}")
        temp_conn = sqlite3.connect(DB_PATH)
        temp_conn.close()
        # Crea las tablas
        conn = sqlite3.connect(DB_PATH)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                phone_number TEXT PRIMARY KEY,
                state TEXT DEFAULT 'ACTIVE',
                context TEXT,
                last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT,
                direction TEXT,
                content TEXT,
                intent TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT,
                name TEXT,
                contact TEXT,
                date_time TIMESTAMP,
                status TEXT DEFAULT 'PENDING',
                google_event_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE TABLE IF NOT EXISTS pending_confirmations (
                phone_number TEXT PRIMARY KEY,
                appointment_data TEXT,
                expires_at TIMESTAMP,
                FOREIGN KEY (phone_number) REFERENCES conversations(phone_number)
            );
            
            CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone_number);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_appointments_datetime ON appointments(date_time);
        ''')
        conn.commit()
        conn.close()
        logger.info("BD creada automáticamente")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error en transacción BD: {e}")
        raise
    finally:
        conn.close()

def save_message(phone, direction, content, intent=None):
    """Guarda mensaje en BD (resiliente)"""
    try:
        with get_db() as conn:
            conn.execute(
                'INSERT INTO messages (phone_number, direction, content, intent) VALUES (?, ?, ?, ?)',
                (phone, direction, content, intent)
            )
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_conversation_history(phone, limit=10):
    """Obtiene historial de conversación desde BD"""
    with get_db() as conn:
        cursor = conn.execute('''
            SELECT content, direction, timestamp 
            FROM messages 
            WHERE phone_number = ?
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (phone, limit))
        
        messages = cursor.fetchall()
        
    # Invierte para mostrar cronológicamente
    history = []
    for msg in reversed(messages):
        prefix = "Usuario" if msg['direction'] == 'incoming' else "Bot"
        history.append(f"{prefix}: {msg['content']}")
    
    return '\n'.join(history)

def update_conversation_state(phone, state, context=None):
    """Actualiza estado de conversación"""
    with get_db() as conn:
        conn.execute('''
            INSERT INTO conversations (phone_number, state, context, last_message_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone_number) DO UPDATE SET
                state = excluded.state,
                context = excluded.context,
                last_message_at = CURRENT_TIMESTAMP
        ''', (phone, state, json.dumps(context) if context else None))

def get_conversation_context(phone):
    """Obtiene contexto de conversación"""
    with get_db() as conn:
        cursor = conn.execute(
            'SELECT context FROM conversations WHERE phone_number = ?',
            (phone,)
        )
        row = cursor.fetchone()
        if row and row['context']:
            return json.loads(row['context'])
    return {}

def save_pending_confirmation(phone, appointment_data):
    """Guarda cita pendiente de confirmación"""
    expires_at = datetime.datetime.now() + datetime.timedelta(minutes=10)
    
    with get_db() as conn:
        conn.execute('''
            INSERT INTO pending_confirmations (phone_number, appointment_data, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET
                appointment_data = excluded.appointment_data,
                expires_at = excluded.expires_at
        ''', (phone, json.dumps(appointment_data), expires_at))
    
    logger.info(f"Confirmación guardada para {phone}")

def get_pending_confirmation(phone):
    """Obtiene cita pendiente de confirmación"""
    with get_db() as conn:
        cursor = conn.execute('''
            SELECT appointment_data 
            FROM pending_confirmations 
            WHERE phone_number = ? AND expires_at > CURRENT_TIMESTAMP
        ''', (phone,))
        
        row = cursor.fetchone()
        if row:
            return json.loads(row['appointment_data'])
    return None

def clear_pending_confirmation(phone):
    """Limpia confirmación pendiente"""
    with get_db() as conn:
        conn.execute('DELETE FROM pending_confirmations WHERE phone_number = ?', (phone,))

def save_appointment(phone, name, contact, dt, google_event_id):
    """Guarda cita confirmada en BD"""
    with get_db() as conn:
        conn.execute('''
            INSERT INTO appointments (phone_number, name, contact, date_time, status, google_event_id)
            VALUES (?, ?, ?, ?, 'CONFIRMED', ?)
        ''', (phone, name, contact, dt, google_event_id))
    
    logger.info(f"Cita guardada: {name} - {dt}")

def send_whatsapp_message(to_phone, message):
    """Envía mensaje de WhatsApp vía Twilio"""
    try:
        msg = twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_phone
        )
        logger.info(f"✓ Mensaje enviado a {to_phone} (SID: {msg.sid})")
        return msg.sid
    except Exception as e:
        logger.error(f"✗ Error enviando mensaje a {to_phone}: {e}")
        return None

# ============================================
# BUFFER DE MENSAJES (mantiene lógica actual)
# ============================================
MESSAGE_BUFFER = defaultdict(lambda: {
    'messages': [],
    'timer': None,
    'lock': threading.Lock(),
    'last_activity': time.time()
})
BUFFER_DELAY = 3
SESSION_CLEANUP_TIME = 600

def cleanup_old_sessions():
    """Limpia sesiones antiguas del buffer"""
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

# ============================================
# PROMPTS
# ============================================
PROMPT_BASE = """
Eres el asistente de Equilibrio.cl (Quiropraxia en Viña del Mar). Sé CONVERSACIONAL y cercano.

**Servicios:** Primera consulta $35k | Sesión $40k | Pack 4 $120k
**Horarios:** Mar/Jue 15-19h | Mié/Vie 10-18h | Sáb 10-13h
**Ubicación:** Av. Reñaca Norte 25, Of. 1506, Viña del Mar

**IMPORTANTE - SISTEMA DE CONFIRMACIÓN:**
Cuando tengas TODOS los datos para agendar (nombre completo, contacto, fecha y hora):
1. NO agendés directamente
2. Mostrá un resumen y pedí confirmación explícita
3. Respondé con: {"intent": "request_confirmation", "data": {...datos...}}

**AGENDAMIENTO:**

ANALIZA TODO el historial de la conversación para extraer:

1. NOMBRE: Busca cualquier mención de nombre y apellido
   - "Nicolas Josue" = nombre: Nicolas, apellido: Josue ✓
   - "Soy Maria Gomez" = Maria Gomez ✓
   - Si solo dice nombre, pide apellido específicamente

2. CONTACTO: Busca números de teléfono (8+ dígitos) O emails
   - "85649247" = teléfono válido ✓
   - "+56912345678" = teléfono válido ✓
   - "usuario@email.com" = email válido ✓

3. FECHA: Extrae del contexto temporal
   - "hoy" = usa fecha actual que te di
   - "mañana" = fecha actual + 1 día
   - "para el 15" = interpreta con mes/año actual

4. HORA: Extrae cualquier mención de hora
   - "a las 17" o "17 horas" = "17:00"
   - "15:30" = "15:30"
   - "tres de la tarde" = "15:00"

Si tienes los 4 datos completos:
{"intent": "request_confirmation", "name": "Nombre Apellido", "contact": "teléfono_o_email", "date": "YYYY-MM-DD", "time": "HH:MM"}

Si falta algo:
{"intent": "schedule", "missing": ["específicamente_lo_que_falta"]}

**ESCALAMIENTO A HUMANO:**
Si el usuario pregunta sobre condiciones médicas serias:
{"intent": "human_support", "reason": "breve razón"}
"""

# ============================================
# PROCESAMIENTO DE MENSAJES
# ============================================
def process_buffered_messages(phone_number):
    """Procesa mensajes agrupados"""
    session = MESSAGE_BUFFER[phone_number]
    
    with session['lock']:
        if not session['messages']:
            return
        
        grouped_message = '\n'.join(session['messages'])
        session['messages'] = []
        session['timer'] = None
    
    # Log de conversación
    conversation_logger.info(f"{phone_number} | IN: {grouped_message}")
    
    # Guarda mensaje entrante
    save_message(phone_number, 'incoming', grouped_message)
    
    # Verifica si hay confirmación pendiente
    pending = get_pending_confirmation(phone_number)
    if pending:
        response_text = handle_confirmation_response(phone_number, grouped_message, pending)
        send_whatsapp_message(phone_number, response_text)
        return
    
    # Detección automática de casos críticos
    if needs_human_intervention(grouped_message):
        support_phone = os.getenv('WHATSAPP_SUPPORT', '+56912345678')
        support_name = os.getenv('SUPPORT_NAME', 'nuestro quiropráctico')
        
        response_text = f"Gracias por compartir. Por la naturaleza de tu consulta, es mejor que hables directamente con {support_name} para evaluarte bien:\n\n📱 {support_phone}\n\n¿O prefieres agendar una evaluación?"
        
        save_message(phone_number, 'outgoing', response_text, 'human_handoff')
        send_whatsapp_message(phone_number, response_text)
        return
    
    try:
        # Obtiene historial desde BD
        history = get_conversation_history(phone_number, limit=10)
        
        today = datetime.datetime.now(TZ)
        current_date_info = f"""
        FECHA ACTUAL: {today.strftime('%Y-%m-%d')} (es {today.strftime('%A %d de %B')})
        Hora actual: {today.strftime('%H:%M')}

        Si el usuario dice:
        - "hoy" = {today.strftime('%Y-%m-%d')}
        - "mañana" = {(today + datetime.timedelta(days=1)).strftime('%Y-%m-%d')}
        """

        ai_prompt = f"""{PROMPT_BASE}
            {current_date_info}
            **HISTORIAL COMPLETO DE LA CONVERSACIÓN:**
            {history}
            
            **NUEVO MENSAJE DEL USUARIO:**
            {grouped_message}
            
            **INSTRUCCIÓN:** Analiza TODO el historial arriba antes de responder.
            """
        
        ai_response = generate_ai_response(ai_prompt)
        response_text = process_ai_response(phone_number, ai_response)
        
        send_whatsapp_message(phone_number, response_text)
        
    except Exception as e:
        logger.error(f"Error procesando mensaje de {phone_number}: {str(e)}", exc_info=True)
        send_whatsapp_message(
            phone_number,
            "Disculpa, hubo un error. ¿Puedes repetir?"
        )

def handle_confirmation_response(phone, message, pending_data):
    """Maneja respuesta a confirmación de cita"""
    message_lower = message.lower().strip()
    
    # Confirmación positiva
    if any(word in message_lower for word in ['si', 'sí', 'confirmo', 'ok', 'vale', 'perfecto', 'correcto']):
        try:
            # Agenda la cita
            result = handle_appointment_booking(pending_data)
            clear_pending_confirmation(phone)
            return result
        except Exception as e:
            logger.error(f"Error al confirmar cita: {e}")
            return "Hubo un error al confirmar. ¿Puedes intentar de nuevo?"
    
    # Cancelación
    elif any(word in message_lower for word in ['no', 'cancelar', 'cambiar', 'modificar']):
        clear_pending_confirmation(phone)
        return "Ok, cancelé esa cita. ¿Querés agendar para otro día/hora?"
    
    # No entendió
    else:
        return "¿Confirmas la cita? Respondé 'Sí' para confirmar o 'No' para cambiar."

def send_whatsapp_message(to_number, message):
    """Envía mensaje vía Twilio"""
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    from_whatsapp = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
    
    if not account_sid or not auth_token:
        logger.error("Credenciales Twilio no configuradas")
        return
    
    try:
        if len(message) > 1500:
            message = message[:1497] + "..."
        
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=message,
            from_=from_whatsapp,
            to=to_number
        )
        
        # Guarda mensaje saliente
        save_message(to_number, 'outgoing', message)
        conversation_logger.info(f"{to_number} | OUT: {message}")
        logger.info(f"✓ Mensaje enviado a {to_number} ({len(message)} chars)")
        
    except Exception as e:
        logger.error(f"✗ Error enviando a {to_number}: {str(e)}")

def process_ai_response(phone_number, ai_response):
    """Procesa respuesta: JSON o texto"""
    try:
        cleaned = ai_response.strip()
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        elif cleaned.startswith('```'):
            cleaned = cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        
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
                    
                    return f"Entiendo que tienes una {reason}. Para darte la mejor orientación, te conectaré con {support_name}:\n\n📱 {support_phone}\n\n¿Prefieres hablar con él directamente?"
                
                elif data.get('intent') == 'request_confirmation':
                    # Guarda datos para confirmar después
                    save_pending_confirmation(phone_number, data)
                    
                    name = data.get('name')
                    contact = data.get('contact')
                    date_str = data.get('date')
                    time_str = data.get('time')
                    
                    # Formatea fecha legible
                    try:
                        dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
                        fecha_legible = dt.strftime('%d/%m/%Y')
                    except:
                        fecha_legible = date_str
                    
                    return f"""📋 Perfecto! Confirmá estos datos:

👤 Nombre: {name}
📞 Contacto: {contact}
📅 Fecha: {fecha_legible}
🕐 Hora: {time_str}
📍 Lugar: Av. Reñaca Norte 25, Of. 1506

¿Todo correcto? Respondé 'Sí' para confirmar o 'No' para cambiar."""
                
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
                            'date': 'fecha',
                            'time': 'hora'
                        }
                        missing_texts = [missing_map.get(f, f) for f in data['missing']]
                        return f"Me falta: {', '.join(missing_texts)} 📅"
                    
            except json.JSONDecodeError:
                pass
        
        if '{"intent"' in cleaned:
            cleaned = cleaned.split('{"intent"')[0].strip()
        
        return cleaned if cleaned else "¿En qué más puedo ayudarte?"
        
    except Exception as e:
        logger.error(f"Error procesando respuesta AI: {str(e)}")
        return "¿Cómo puedo ayudarte?"

def generate_ai_response(prompt):
    """Genera respuesta con Gemini"""
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Error Gemini: {str(e)}")
        return "Disculpa, problemas técnicos. Intenta de nuevo."

# ============================================
# FUNCIONES DE CALENDARIO 
# ============================================
def get_available_slots(date_str):
    """Obtiene horarios disponibles para una fecha"""
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        dt = TZ.localize(dt)
        weekday = dt.weekday()
        
        if weekday == 0 or weekday == 6:
            return None
        elif weekday in [1, 3]:
            slots = [(15, 0), (16, 0), (17, 0), (18, 0)]
        elif weekday in [2, 4]:
            slots = [(10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0)]
        elif weekday == 5:
            slots = [(10, 0), (11, 0), (12, 0)]
        
        available = []
        for hour, minute in slots:
            slot_dt = dt.replace(hour=hour, minute=minute)
            end_dt = slot_dt + datetime.timedelta(hours=1)
            
            if slot_dt > datetime.datetime.now(TZ) and not check_freebusy(slot_dt, end_dt):
                available.append(f"{hour:02d}:{minute:02d}")
        
        return available
    except Exception as e:
        logger.error(f"Error obteniendo slots: {e}")
        return None

def handle_appointment_booking(data):
    try:
        name = data.get('name')
        contact = data.get('contact')
        date_str = data.get('date')
        time_str = data.get('time')
        
        if len(name.split()) < 2:
            return "Por favor, dame tu nombre y apellido completo 😊"
        
        contact_clean = contact.replace('+', '').replace(' ', '').replace('-', '')
        is_phone = contact_clean.isdigit() and len(contact_clean) >= 8
        is_email = re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', contact) is not None
        
        if not (is_phone or is_email):
            return "Necesito un teléfono válido (8+ dígitos) o un email 📱"
        
        logger.info(f"Agendando: {name} | {contact} | {date_str} | {time_str}")
        
        time_str = time_str.replace('.', ':').replace(' ', '')
        if ':' not in time_str and len(time_str) <= 2:
            time_str = f"{time_str}:00"

        date_str = date_str.replace('/', '-')
        if date_str.count('-') == 2:
            parts = date_str.split('-')
            if len(parts[0]) == 2:
                date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
        
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return "Error en fecha/hora. Usa: YYYY-MM-DD y HH:MM"
        
        dt = TZ.localize(dt)
        end_dt = dt + datetime.timedelta(hours=1)
        
        error = validate_business_hours(dt)
        if error:
            return error
        
        if check_freebusy(dt, end_dt):
            return f"❌ {date_str} a las {time_str} ya está ocupado.\n¿Otro horario?"
        
        # Crea cita y guarda en BD
        event_id = create_appointment(name, contact, dt)
        save_appointment(data.get('phone', 'unknown'), name, contact, dt, event_id)
        
        fecha_formato = dt.strftime("%d/%m/%Y")
        return f"✅ ¡Listo {name}!\n📅 {fecha_formato} a las {time_str}\n📍 Av. Reñaca Norte 25, Of. 1506\n\n¡Te esperamos!"
        
    except Exception as e:
        logger.error(f"Error agendando: {str(e)}", exc_info=True)
        return "Error al agendar. Llámanos: +56 9 XXXX XXXX"

def validate_business_hours(dt):
    """Valida horarios de negocio"""
    weekday = dt.weekday()
    hour = dt.hour
    
    now = datetime.datetime.now(TZ)
    if dt < now:
        return "❌ Esa fecha/hora ya pasó"
    
    if weekday == 0:
        return "❌ Cerrados los lunes"
    elif weekday == 6:
        return "❌ Cerrados los domingos"
    elif weekday in [1, 3]:
        if not (15 <= hour < 19):
            return "❌ Mar/Jue atendemos 15:00-19:00"
    elif weekday in [2, 4]:
        if not (10 <= hour < 18):
            return "❌ Mié/Vie atendemos 10:00-18:00"
    elif weekday == 5:
        if not (10 <= hour < 13):
            return "❌ Sábados 10:00-13:00"
    
    return None

def check_freebusy(start_dt, end_dt):
    """Verifica disponibilidad en calendario"""
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
        logger.error(f"Error calendario: {str(e)}")
        return False

def create_appointment(name, contact, dt):
    """Crea evento en Google Calendar"""
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
        logger.info(f"✓ Cita creada: {name} - {dt.strftime('%Y-%m-%d %H:%M')}")
        return result.get('id')
        
    except Exception as e:
        logger.error(f"✗ Error creando cita: {str(e)}")
        raise

# ============================================
# RUTAS FLASK
# ============================================
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Webhook de Twilio"""
    incoming_msg = request.values.get('Body', '').strip()
    from_phone = request.values.get('From', '')
    
    if not incoming_msg or not from_phone:
        return '', 200
    
    logger.info(f"→ Mensaje de {from_phone}: {incoming_msg}")
    
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

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return {
        'status': 'ok',
        'service': 'equilibrio-bot',
        'timestamp': datetime.datetime.now(TZ).isoformat(),
        'db_path': DB_PATH
    }, 200

@app.route('/stats', methods=['GET'])
def stats():
    """Endpoint de estadísticas básicas"""
    try:
        with get_db() as conn:
            total_conversations = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
            total_messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            total_appointments = conn.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]
            
            return {
                'total_conversations': total_conversations,
                'total_messages': total_messages,
                'total_appointments': total_appointments,
                'timestamp': datetime.datetime.now(TZ).isoformat()
            }, 200
    except Exception as e:
        logger.error(f"Error obteniendo stats: {e}")
        return {'error': str(e)}, 500

# ============================================
# INICIALIZACIÓN
# ============================================
try:
    init_db()
    logger.info("✅ Base de datos inicializada correctamente")
except Exception as e:
    logger.error(f"❌ Error inicializando BD: {e}")
    # No raise - permite que el bot arranque igual

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"🚀 Equilibrio Bot iniciando en puerto {port}...")
    logger.info(f"📁 Base de datos: {DB_PATH}")
    app.run(host='0.0.0.0', port=port, debug=False)