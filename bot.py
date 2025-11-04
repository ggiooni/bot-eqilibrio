from flask import Flask, request
from twilio.rest import Client
import os
from dotenv import load_dotenv
import google.generativeai as genai
from google.generativeai.types import Tool, FunctionDeclaration
from google.generativeai.types import content_types as S
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
from twilio.request_validator import RequestValidator

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
validator = RequestValidator(TWILIO_AUTH_TOKEN)
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
# --- DEFINICIÓN DE HERRAMIENTAS DE AGENDAMIENTO --- (Cambio: Nueva sección añadida)
# ============================================

# Herramienta 1: Agendar UNA cita
book_single_appointment_tool = {
    "name": "book_single_appointment",
    "description": "Agenda una (1) cita única para un paciente.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Nombre y apellido completo del paciente"
            },
            "contact": {
                "type": "string",
                "description": "Teléfono (ej: 912345678) o email del paciente"
            },
            "date": {
                "type": "string",
                "description": "Fecha de la cita en formato YYYY-MM-DD"
            },
            "time": {
                "type": "string",
                "description": "Hora de la cita en formato HH:MM"
            },
        },
        "required": ["name", "contact", "date", "time"]
    }
}

# Herramienta 2: Agendar MÚLTIPLES citas
book_multiple_appointments_tool = {
    "name": "book_multiple_appointments",
    "description": "Agenda un paquete o serie de múltiples citas (ej: 4 sesiones) para un mismo paciente.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Nombre y apellido completo del paciente"
            },
            "contact": {
                "type": "string",
                "description": "Teléfono (ej: 912345678) o email del paciente"
            },
            "appointments": {
                "type": "array",
                "description": "Una lista de las citas a agendar.",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Fecha en YYYY-MM-DD"
                        },
                        "time": {
                            "type": "string",
                            "description": "Hora en HH:MM"
                        },
                    },
                    "required": ["date", "time"]
                }
            }
        },
        "required": ["name", "contact", "appointments"]
    }
}

# Crea el set de herramientas
appointment_tools = Tool(
    function_declarations=[book_single_appointment_tool, book_multiple_appointments_tool]
)
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

        # Detectar rechazos o preferencias en mensaje
        if re.search(r'\b(no|no quiero|diferentes|cada \d+ d[ií]as|semanal|mensual)\b', user_message.lower()):
            context['state'] = 'asking_preferences'  # Marca estado para que prompt sepa
            context['user_preferences'] = user_message  # Guarda lo que dijo
            update_conversation_state(from_phone, 'asking_preferences', context)
        
        # PROMPT MEJORADO CON EJEMPLOS REALES (Cambio: Modificado para usar herramientas en lugar de JSON)
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
+56 9 8791 8694

**MÉTODO EQUILIBRIO:**
El Método Equilibrio es una técnica quiropráctica que trabaja con la columna vertebral, sistema nervioso y postura para mejorar el bienestar general del paciente.

🤖 CÓMO AGENDAR CITAS:
- Para paquetes (ej. 4-8 sesiones), sugiere horarios distribuidos: semanales (ej. cada miércoles), cada X días, mensuales, etc. Evita mismo día a menos que sea pedido.
- Ejemplo para 4 sesiones: "Te sugiero una cada semana: Mié 5/11 10:00, Mié 12/11 10:00, Mié 19/11 10:00, Mié 26/11 10:00."
- Valida disponibilidad en rango (próximos días/semanas/meses) y ajusta si esta ocupado.
- Si usuario menciona frecuencia (ej. cada 4 días, mensual), calcula fechas acordemente.
- Manejo de rechazos: Si 'no' o duda, responde: "Entiendo, ¿qué días/horas te acomodan mejor? ¿Prefieres semanal, cada X días, o en un mes específico?" Luego, usa tool si confirma.

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
- Próximos 7 días: {json.dumps(get_available_slots_in_range(datetime.datetime.now(TZ), datetime.datetime.now(TZ) + datetime.timedelta(days=7)))}
- Próximos 30 días: Resume disponibles (usa rangos para multi-sesiones, ej. 'Miércoles disponibles: 5/11, 12/11, 19/11, 26/11').

📝 HISTORIAL: {history}
💾 CONTEXTO: {json.dumps(context)}
⏳ PENDIENTE: {json.dumps(pending)}

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

🔧 CÓMO AGENDAR (USO DE HERRAMIENTAS):  

PASO 1: Recopila nombre, contacto, fecha y hora.
PASO 2: Muestra el resumen al usuario y pide confirmación explícita (Sí/No).
PASO 3: SOLO SI EL USUARIO CONFIRMA ("Sí", "Confirmo", "Dale"), usarás una herramienta.

-   **Para 1 cita:** Llama a la herramienta `book_single_appointment` con los datos.
-   **Para varias citas (ej: "Quiero 4 sesiones"):** Debes primero encontrar 4 horarios disponibles (ej: "Miércoles 10:00, Jueves 11:00..."), mostrarlos al usuario, y si confirma, llamar a la herramienta `book_multiple_appointments` con la *lista* de citas.
-   **NUNCA llames a la herramienta sin la confirmación explícita del usuario.** Si el usuario solo está preguntando, responde como texto.

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
Bot: {{"action": "book_appointment",  # Nota: Esto se cambia internamente por la herramienta
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
Bot: {{"action": "book_appointment",  # Nota: Esto se cambia internamente por la herramienta
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

        
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',  # Gemini 2.5 Flash experimental
            generation_config={
                'temperature': 0.3,  
                'top_p': 0.95,
                'top_k': 40,
                'max_output_tokens': 800,
            },
            tools=[appointment_tools]  
        )
        
        response = model.generate_content(
            f"{system_prompt}\n\nMensaje del usuario:\n{user_message}"
        )
        # Manejo de errores en respuesta
        if not response.candidates or not response.candidates[0].content.parts:
            logger.error(f"Respuesta de Gemini vacía o inválida para mensaje: {user_message}")
            return "Disculpa, algo salió mal al procesar tu solicitud. ¿Puedes repetir?"
        
        bot_response_part = response.candidates[0].content.parts[0]

        # Revisa si Gemini pidió llamar a una herramienta
        if hasattr(bot_response_part, 'function_call') and bot_response_part.function_call:
            function_call = bot_response_part.function_call
            function_name = function_call.name
            args = function_call.args
            
            logger.info(f"🤖 Gemini solicita llamar a la herramienta: {function_name}")
            
            # -----------------------------------------------
            # CASO 1: AGENDAR CITA ÚNICA
            # -----------------------------------------------
            if function_name == "book_single_appointment":
                try:
                    # Los argumentos ya vienen como un dict, no más JSON.loads
                    appointment_data = {
                        'name': args.get('name'),
                        'contact': args.get('contact'),
                        'date': args.get('date'),
                        'time': args.get('time'),
                        'phone': from_phone # Añade el 'from_phone'
                    }
                    
                    # Llama a tu función de agendamiento existente
                    result = handle_appointment_booking(appointment_data)
                    clear_pending_confirmation(from_phone)
                    return result
                
                except Exception as e:
                    logger.error(f"Error procesando 'book_single_appointment': {e}")
                    return "Hubo un error al procesar tu cita. ¿Puedes confirmar nuevamente?"
            
            # -----------------------------------------------
            # CASO 2: AGENDAR CITAS MÚLTIPLES (¡NUEVO!)
            # -----------------------------------------------
            elif function_name == "book_multiple_appointments":
                try:
                    name = args.get('name')
                    contact = args.get('contact')
                    appointments_list = args.get('appointments', [])
                    
                    if not appointments_list:
                        return "Error: Se intentó agendar múltiples citas pero no se encontraron fechas/horas."
                    
                    results_messages = []
                    
                    # Itera sobre la lista de citas que dio Gemini
                    for appt in appointments_list:
                        appointment_data = {
                            'name': name,
                            'contact': contact,
                            'date': appt.get('date'),
                            'time': appt.get('time'),
                            'phone': from_phone
                        }
                        # Llama a tu función de booking POR CADA CITA
                        result = handle_appointment_booking(appointment_data)
                        results_messages.append(result)
                    
                    clear_pending_confirmation(from_phone)
                    # Devuelve un resumen de todas las citas agendadas
                    return f"¡Agendamiento múltiple completado!\n\n" + "\n\n".join(results_messages)
    
                except Exception as e:
                    logger.error(f"Error procesando 'book_multiple_appointments': {e}")
                    return "Hubo un error al procesar tus citas. Por favor, inténtalo de nuevo."
    
            # Si es otra herramienta que no conocemos
            else:
                logger.warning(f"Herramienta desconocida: {function_name}")
                return "Disculpa, tuve un problema interno (Herramienta desconocida)."
    
        # Si no hay 'function_call', es una respuesta de texto normal
        else:
            bot_response = bot_response_part.text.strip()
            
            # Aquí puedes mantener tu lógica de 'pending_confirmation'
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
    
def get_available_slots_in_range(start_date, end_date):
    """Obtiene slots disponibles en un rango de fechas"""
    current = start_date
    available = {}
    while current <= end_date:
        slots = get_available_slots(current)
        if slots:
            available[current.strftime('%Y-%m-%d')] = slots
        current += datetime.timedelta(days=1)
    return available

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
    """Webhook de Twilio CON VALIDACIÓN DE SEGURIDAD"""
    # --- INICIO DE BLOQUE DE SEGURIDAD ---
    # Obtén la URL completa, los datos del formulario y la firma de la cabecera
    url = request.url
    post_data = request.form.to_dict()
    twilio_signature = request.headers.get('X-Twilio-Signature', '')

    # Valida la petición
    if not validator.validate(url, post_data, twilio_signature):
        logger.warning(f"ALERTA DE SEGURIDAD: Petición no validada desde {request.remote_addr}")
        return 'Webhook no autorizado', 403 # 403 Forbidden
    # --- FIN DE BLOQUE DE SEGURIDAD ---
    incoming_msg = request.values.get('Body', '').strip()
    from_phone = request.values.get('From', '')
    
    if not incoming_msg or not from_phone:
        return '', 200
    
    # --- INICIO DE LOG ANÓNIMO ---
    try:
        # Obtiene solo los últimos 4 dígitos para depuración
        phone_ending = from_phone[-4:] 
        logger.info(f"→ [Validado] Mensaje de (***{phone_ending}): [MENSAJE RECIBIDO]")
    except Exception:
        # Fallback por si 'from_phone' es inválido
        logger.info(f"→ [Validado] Mensaje de (***ANONIMO): [MENSAJE RECIBIDO]")
    # --- FIN DE LOG ANÓNIMO ---
    
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
        'model': 'gemini-2.5-flash',
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
                'model': 'gemini-2.5-flash',
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
    logger.info(f"🤖 Modelo: Gemini 2.5 Flash Experimental")
    logger.info(f"📊 Base de datos: Supabase (PostgreSQL)")
    logger.info(f"🏢 Cliente: {CLIENT_ID}")
    app.run(host='0.0.0.0', port=port, debug=False)