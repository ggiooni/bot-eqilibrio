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
import psycopg2
from psycopg2.extras import RealDictCursor
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
# GESTIÓN DE BASE DE DATOS (PostgreSQL/Supabase)
# ============================================
DATABASE_URL = os.getenv('DATABASE_URL')
CLIENT_ID = os.getenv('CLIENT_ID')

if not DATABASE_URL:
    raise ValueError("ERROR: DATABASE_URL no configurado")
if not CLIENT_ID:
    raise ValueError("ERROR: CLIENT_ID no configurado")

@contextmanager
def get_db():
    """Context manager para conexión a Supabase (PostgreSQL)"""
    conn = psycopg2.connect(DATABASE_URL)
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
    """Guarda mensaje en BD con client_id"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Primero obtiene o crea la conversación
            cursor.execute('''
                INSERT INTO conversations (client_id, phone_number, last_message_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (client_id, phone_number) 
                DO UPDATE SET last_message_at = NOW()
                RETURNING id
            ''', (CLIENT_ID, phone))
            
            conversation_id = cursor.fetchone()[0]
            
            # Guarda el mensaje
            cursor.execute('''
                INSERT INTO messages (conversation_id, client_id, phone_number, direction, content, intent)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (conversation_id, CLIENT_ID, phone, direction, content, intent))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_conversation_history(phone, limit=10):
    """Obtiene historial de conversación desde BD"""
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT content, direction, timestamp 
            FROM messages 
            WHERE phone_number = %s AND client_id = %s
            ORDER BY timestamp DESC 
            LIMIT %s
        ''', (phone, CLIENT_ID, limit))
        
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
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO conversations (client_id, phone_number, state, context, last_message_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (client_id, phone_number) DO UPDATE SET
                state = EXCLUDED.state,
                context = EXCLUDED.context,
                last_message_at = NOW()
        ''', (CLIENT_ID, phone, state, json.dumps(context) if context else None))

def get_conversation_context(phone):
    """Obtiene contexto de conversación"""
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            'SELECT context FROM conversations WHERE phone_number = %s AND client_id = %s',
            (phone, CLIENT_ID)
        )
        row = cursor.fetchone()
        if row and row['context']:
            return json.loads(row['context'])
    return {}

def save_pending_confirmation(phone, appointment_data):
    """Guarda cita pendiente de confirmación"""
    expires_at = datetime.datetime.now() + datetime.timedelta(minutes=10)
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pending_confirmations (client_id, phone_number, appointment_data, expires_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (client_id, phone_number) DO UPDATE SET
                appointment_data = EXCLUDED.appointment_data,
                expires_at = EXCLUDED.expires_at
        ''', (CLIENT_ID, phone, json.dumps(appointment_data), expires_at))
    
    logger.info(f"Confirmación guardada para {phone}")

def get_pending_confirmation(phone):
    """Obtiene cita pendiente de confirmación"""
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT appointment_data 
            FROM pending_confirmations 
            WHERE phone_number = %s AND client_id = %s AND expires_at > NOW()
        ''', (phone, CLIENT_ID))
        
        row = cursor.fetchone()
        if row:
            data = row['appointment_data']
            # Si ya es dict, devolver directo; si es string, parsear
            return data if isinstance(data, dict) else json.loads(data)
    return None

def clear_pending_confirmation(phone):
    """Limpia confirmación pendiente"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM pending_confirmations WHERE phone_number = %s AND client_id = %s',
            (phone, CLIENT_ID)
        )

def save_appointment(phone, name, contact, appointment_time, event_id=None):
    """Guarda cita en BD"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Obtener conversation_id (para vincular profesionalmente)
        cursor.execute('''
            SELECT id FROM conversations 
            WHERE client_id = %s AND phone_number = %s
        ''', (CLIENT_ID, phone))
        row = cursor.fetchone()
        conversation_id = row[0] if row else None  # NULL si no existe (permitido)
        if not conversation_id:
            logger.warning(f"No se encontró conversación para {phone}, usando conversation_id=NULL")
        
        # INSERT original (ya profesional) + conversation_id
        cursor.execute('''
            INSERT INTO appointments (client_id, conversation_id, phone_number, patient_name, contact_info, appointment_time, google_event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (CLIENT_ID, conversation_id, phone, name, contact, appointment_time, event_id))
def handle_appointment_booking(data):
    try:
        # ... (código existente)
        event_id = create_appointment(name, contact, dt)
        save_appointment(data.get('phone', 'unknown'), name, contact, dt, event_id)
        # ... (return éxito)
    except psycopg2.errors.UndefinedColumn as e:
        logger.error(f"Error de esquema en BD: {e}")
        return "Error en la base de datos (posible mismatch de columnas). Contacta al admin o llama al +56 9 7533 2088."
    except psycopg2.Error as e:
        logger.error(f"Error BD al agendar: {e}", exc_info=True)
        return "Cita creada en calendario, pero problema en BD. Llama al +56 9 7533 2088 para confirmar."
    except Exception as e:
        # Manejo existente

# ============================================
# BUFFER DE MENSAJES (agrupamiento inteligente)
# ============================================
MESSAGE_BUFFER = defaultdict(lambda: {
    'messages': [],
    'timer': None,
    'lock': threading.Lock(),
    'last_activity': time.time()
})

BUFFER_DELAY = 5  # segundos de espera

def cleanup_old_sessions():
    """Limpia sesiones inactivas > 30 min"""
    now = time.time()
    timeout = 30 * 60
    to_remove = [
        phone for phone, session in MESSAGE_BUFFER.items()
        if now - session['last_activity'] > timeout
    ]
    for phone in to_remove:
        if MESSAGE_BUFFER[phone]['timer']:
            MESSAGE_BUFFER[phone]['timer'].cancel()
        del MESSAGE_BUFFER[phone]
        logger.info(f"Sesión limpiada: {phone}")

def process_buffered_messages(from_phone):
    """Procesa mensajes agrupados"""
    session = MESSAGE_BUFFER[from_phone]
    
    with session['lock']:
        if not session['messages']:
            return
        
        combined_message = '\n'.join(session['messages'])
        session['messages'].clear()
        session['timer'] = None
    
    logger.info(f"📦 Procesando {len(session['messages'])} mensajes de {from_phone}")
    
    # Guarda mensaje entrante
    save_message(from_phone, 'incoming', combined_message)
    
    # Log conversacional
    conversation_logger.info(f"USER ({from_phone}): {combined_message}")
    
    # Genera respuesta
    response = generate_response(combined_message, from_phone)
    
    # Guarda respuesta
    save_message(from_phone, 'outgoing', response)
    conversation_logger.info(f"BOT: {response}")
    
    # Envía por Twilio
    send_whatsapp_message(from_phone, response)

# ============================================
# MODELO GEMINI 2.5 CON PROMPT MEJORADO
# ============================================

def generate_response(user_message, from_phone):
    """
    Genera respuesta usando Gemini 2.5 Flash con prompt optimizado
    """
    try:
        # Obtener contexto conversacional
        history = get_conversation_history(from_phone, limit=15)
        context = get_conversation_context(from_phone)
        
        # Verificar si hay confirmación pendiente
        pending = get_pending_confirmation(from_phone)
        
        # Verificar disponibilidad de horarios para hoy/mañana
        available_today = get_available_slots(datetime.datetime.now(TZ))
        available_tomorrow = get_available_slots(datetime.datetime.now(TZ) + datetime.timedelta(days=1))
        
        # PROMPT MEJORADO CON EJEMPLOS REALES
        system_prompt = f"""Eres el asistente virtual de EQUILIBRIO, centro quiropráctico especializado en el Método Equilibrio.

🎯 TU MISIÓN: 
- Responder consultas sobre precios, servicios y horarios
- Agendar citas de forma conversacional y natural
- Derivar casos médicos complejos al quiropráctico

📋 INFORMACIÓN DEL CENTRO:

**PRECIOS:**
- Primera consulta: $35.000
- Sesiones siguientes: $40.000

**HORARIOS DE ATENCIÓN:**
- Martes y Jueves: 15:00 - 19:00
- Miércoles y Viernes: 10:00 - 18:00
- Sábados: 10:00 - 13:00
- Domingos y Lunes: CERRADOS

**DIRECCIÓN:**
Av. Reñaca Norte 25, Oficina 1506, Viña del Mar

**TELÉFONO:**
+56 9 7533 2088

**MÉTODO EQUILIBRIO:**
El Método Equilibrio es una técnica quiropráctica que trabaja con la columna vertebral, sistema nervioso y postura para mejorar el bienestar general del paciente.

🤖 CÓMO AGENDAR CITAS:

PASO 1: Si el usuario quiere agendar, pregunta PRIMERO por nombre completo
Ejemplo: "¿Cuál es tu nombre completo?" (espera respuesta)

PASO 2: Luego pregunta teléfono o email
Ejemplo: "Perfecto Juan, ¿tu teléfono o email?" (espera respuesta)

PASO 3: Si el usuario ya dio fecha/hora, valida disponibilidad
Si NO dio fecha/hora, ofrece horarios disponibles

PASO 4: Muestra resumen y PIDE CONFIRMACIÓN EXPLÍCITA
Ejemplo: 
"📋 Resumen de tu cita:
• Nombre: Juan Pérez
• Fecha: Miércoles 20/03/2024
• Hora: 16:00
• Teléfono: 912345678
• Lugar: Av. Reñaca Norte 25, Of. 1506

¿Confirmas para agendar? (Responde Sí o No)"

PASO 5: SOLO si confirma, responde con el JSON de agendamiento

⚠️ CASOS MÉDICOS COMPLEJOS - DERIVAR AL QUIROPRÁCTICO:
Si detectas alguna de estas condiciones, NO intentes agendar directamente:
- Embarazo
- Cirugías recientes (<6 meses)
- Fracturas
- Osteoporosis severa
- Cáncer activo
- Problemas neurológicos graves
- Dolor intenso repentino

En estos casos, responde:
"Por tu condición, es importante que hables directamente con nuestro quiropráctico para evaluar tu caso. Te recomiendo llamar al +56 9 7533 2088 para coordinar una evaluación personalizada."

📊 DISPONIBILIDAD ACTUAL:
- Hoy: {', '.join(available_today) if available_today else 'Sin disponibilidad'}
- Mañana: {', '.join(available_tomorrow) if available_tomorrow else 'Sin disponibilidad'}

📝 HISTORIAL DE CONVERSACIÓN:
{history if history else "Primera interacción"}

💾 CONTEXTO ACTUAL:
{json.dumps(context, ensure_ascii=False) if context else "Sin contexto previo"}

⏳ CONFIRMACIÓN PENDIENTE:
{json.dumps(pending, ensure_ascii=False) if pending else "Ninguna"}

🎨 TONO Y ESTILO:
- Amigable y cercano, usando emojis moderadamente
- Profesional pero no robótico
- Respuestas cortas y claras (máximo 3-4 líneas por respuesta)
- Si no estás seguro de algún dato, pide aclaración en lugar de adivinar

📌 REGLAS CRÍTICAS:
1. NUNCA inventes fechas u horarios - usa solo los disponibles
2. NUNCA supongas el nombre completo del usuario - pregunta siempre
3. NUNCA agendes sin confirmación explícita del usuario
4. Si falta nombre o contacto, pregúntalo antes de mostrar el resumen
5. Valida que el nombre tenga nombre Y apellido (mínimo 2 palabras)
6. Valida que el contacto sea teléfono (8+ dígitos) o email válido

🔧 FORMATO DE RESPUESTA PARA AGENDAR:
SOLO cuando el usuario confirme "sí" o equivalente después de ver el resumen, responde:

{{
  "action": "book_appointment",
  "name": "Nombre Apellido",
  "contact": "912345678",
  "date": "2024-03-20",
  "time": "16:00"
}}

❌ EJEMPLOS DE CONVERSACIONES FALLIDAS (EVITAR):

**Falla 1: Agendar sin confirmación**
Usuario: "Quiero hora para mañana a las 3"
❌ Bot: {{..."action": "book_appointment"...}}
✅ Bot: "¿Cuál es tu nombre completo?"

**Falla 2: Suponer nombre completo**
Usuario: "Juan"
❌ Bot: {{..."name": "Juan"...}}
✅ Bot: "Hola Juan! ¿Cuál es tu apellido?"

**Falla 3: No validar contacto**
Usuario: "123"
❌ Bot: {{..."contact": "123"...}}
✅ Bot: "Necesito un teléfono válido (8+ dígitos) o un email 📱"

✅ EJEMPLOS DE CONVERSACIONES EXITOSAS:

**Ejemplo 1: Agendamiento completo**
Usuario: "Hola, quiero agendar para mañana"
Bot: "¡Hola! Claro, te ayudo a agendar. ¿Cuál es tu nombre completo?"
Usuario: "María González"
Bot: "Perfecto María, ¿tu teléfono o email?"
Usuario: "912345678"
Bot: "¿A qué hora prefieres? Mañana tengo disponible: 10:00, 11:00, 12:00"
Usuario: "A las 11"
Bot: "📋 Resumen de tu cita:
• Nombre: María González
• Fecha: Miércoles 20/03/2024
• Hora: 11:00
• Teléfono: 912345678
• Lugar: Av. Reñaca Norte 25, Of. 1506

¿Confirmas para agendar?"
Usuario: "Sí"
Bot: {{
  "action": "book_appointment",
  "name": "María González",
  "contact": "912345678",
  "date": "2024-03-20",
  "time": "11:00"
}}

**Ejemplo 2: Usuario da toda la info junta**
Usuario: "Soy Pedro Silva, mi teléfono es 987654321, quiero hora para el miércoles 20 a las 16:00"
Bot: "Perfecto Pedro! 

📋 Resumen de tu cita:
• Nombre: Pedro Silva
• Fecha: Miércoles 20/03/2024
• Hora: 16:00
• Teléfono: 987654321
• Lugar: Av. Reñaca Norte 25, Of. 1506

¿Confirmas para agendar?"
Usuario: "Dale"
Bot: {{
  "action": "book_appointment",
  "name": "Pedro Silva",
  "contact": "987654321",
  "date": "2024-03-20",
  "time": "16:00"
}}

**Ejemplo 3: Caso médico complejo**
Usuario: "Hola, estoy embarazada y me duele mucho la espalda"
Bot: "Hola! Por tu condición de embarazo, es importante que hables directamente con nuestro quiropráctico para evaluar tu caso de forma personalizada. Te recomiendo llamar al +56 9 7533 2088 para coordinar una evaluación adecuada. ¿Te ayudo con algo más?"

**Ejemplo 4: Solo consulta de precio**
Usuario: "Cuánto cuesta la consulta?"
Bot: "La primera consulta cuesta $35.000 y las sesiones siguientes $40.000. ¿Quieres agendar una cita?"

🔄 FECHA/HORA ACTUAL: {datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}

Ahora, responde al mensaje del usuario de forma natural y siguiendo todas estas reglas."""

        # Llamada a Gemini 2.5 Flash (nuevo modelo)
        model = genai.GenerativeModel(
            model_name='gemini-2.0-flash-exp',  # Gemini 2.5 Flash experimental
            generation_config={
                'temperature': 0.3,  # Más determinista para mayor precisión
                'top_p': 0.95,
                'top_k': 40,
                'max_output_tokens': 800,
            }
        )
        
        response = model.generate_content(
            f"{system_prompt}\n\nMensaje del usuario:\n{user_message}"
        )
        
        bot_response = response.text.strip()
        
        # Detectar si es comando de agendamiento
        if '{"action": "book_appointment"' in bot_response or '"action":"book_appointment"' in bot_response:
            try:
                # Extraer JSON de la respuesta
                json_match = re.search(r'\{[^}]+\}', bot_response, re.DOTALL)
                if json_match:
                    appointment_data = json.loads(json_match.group())
                    
                    if appointment_data.get('action') == 'book_appointment':
                        # Procesar agendamiento
                        appointment_data['phone'] = from_phone
                        result = handle_appointment_booking(appointment_data)
                        clear_pending_confirmation(from_phone)
                        return result
            except Exception as e:
                logger.error(f"Error procesando JSON de agendamiento: {e}")
                return "Hubo un error al procesar tu cita. ¿Puedes confirmar nuevamente?"
        
        # Detectar si es un resumen pre-confirmación
        if '¿Confirmas para agendar?' in bot_response or '¿Confirmas?' in bot_response:
            # Extraer datos del resumen para guardar en pending_confirmations
            try:
                # Buscar datos en el resumen
                name_match = re.search(r'Nombre:\s*([^\n]+)', bot_response)
                date_match = re.search(r'Fecha:\s*([^\n]+)', bot_response)
                time_match = re.search(r'Hora:\s*(\d{1,2}:\d{2})', bot_response)
                contact_match = re.search(r'(?:Teléfono|Email):\s*([^\n]+)', bot_response)
                
                if name_match and date_match and time_match and contact_match:
                    # Parsear fecha
                    date_text = date_match.group(1).strip()
                    # Intentar extraer fecha en formato DD/MM/YYYY
                    date_number_match = re.search(r'(\d{2})/(\d{2})/(\d{4})', date_text)
                    if date_number_match:
                        day, month, year = date_number_match.groups()
                        date_formatted = f"{year}-{month}-{day}"
                    else:
                        # Usar fecha sugerida del contexto o mañana por defecto
                        date_formatted = (datetime.datetime.now(TZ) + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                    
                    pending_data = {
                        'name': name_match.group(1).strip(),
                        'contact': contact_match.group(1).strip(),
                        'date': date_formatted,
                        'time': time_match.group(1).strip(),
                        'phone': from_phone
                    }
                    save_pending_confirmation(from_phone, pending_data)
                    logger.info(f"Confirmación pendiente guardada: {pending_data}")
            except Exception as e:
                logger.error(f"Error guardando confirmación pendiente: {e}")
        
        # Detectar confirmación del usuario
        if pending and re.search(r'\b(s[ií]|confirmo|dale|ok|okay|correcto)\b', user_message.lower()):
            # Usuario confirmó, procesar agendamiento
            result = handle_appointment_booking(pending)
            clear_pending_confirmation(from_phone)
            return result
        
        return bot_response
        
    except Exception as e:
        logger.error(f"Error en Gemini: {str(e)}", exc_info=True)
        return "Disculpa, tuve un problema. ¿Puedes repetir tu consulta?"

def send_whatsapp_message(to_phone, message):
    """Envía mensaje por Twilio"""
    try:
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_phone
        )
        logger.info(f"← Mensaje enviado a {to_phone}")
    except Exception as e:
        logger.error(f"Error enviando mensaje: {str(e)}")

def get_available_slots(date):
    """Obtiene horarios disponibles para una fecha"""
    try:
        dt = date.replace(hour=0, minute=0, second=0, microsecond=0)
        if dt.tzinfo is None:
            dt = TZ.localize(dt)
        
        weekday = dt.weekday()
        
        # Cerrado lunes y domingos
        if weekday == 0 or weekday == 6:
            return []
        
        # Definir slots según día
        if weekday in [1, 3]:  # Mar/Jue
            slots = [(15, 0), (16, 0), (17, 0), (18, 0)]
        elif weekday in [2, 4]:  # Mié/Vie
            slots = [(10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0)]
        elif weekday == 5:  # Sáb
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
        return "Error al agendar. Llámanos: +56 9 7533 2088"

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
        'model': 'gemini-2.0-flash-exp',
        'timestamp': datetime.datetime.now(TZ).isoformat(),
        'database': 'supabase'
    }, 200

@app.route('/stats', methods=['GET'])
def stats():
    """Endpoint de estadísticas básicas"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM conversations WHERE client_id = %s', (CLIENT_ID,))
            total_conversations = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM messages WHERE client_id = %s', (CLIENT_ID,))
            total_messages = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM appointments WHERE client_id = %s', (CLIENT_ID,))
            total_appointments = cursor.fetchone()[0]
            
            return {
                'total_conversations': total_conversations,
                'total_messages': total_messages,
                'total_appointments': total_appointments,
                'model': 'gemini-2.0-flash-exp',
                'timestamp': datetime.datetime.now(TZ).isoformat()
            }, 200
    except Exception as e:
        logger.error(f"Error obteniendo stats: {e}")
        return {'error': str(e)}, 500

# ============================================
# INICIALIZACIÓN
# ============================================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"🚀 Equilibrio Bot v2.0 iniciando en puerto {port}...")
    logger.info(f"🤖 Modelo: Gemini 2.0 Flash Experimental")
    logger.info(f"📊 Base de datos: Supabase (PostgreSQL)")
    logger.info(f"🏢 Cliente: {CLIENT_ID}")
    app.run(host='0.0.0.0', port=port, debug=False)
