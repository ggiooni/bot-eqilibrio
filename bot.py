from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
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
from collections import defaultdict

load_dotenv()

app = Flask(__name__)

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

# Configuración de Google Calendar con service account
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'service_account.json')
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = '059bad589de3d4b2457841451a3939ba605411559b7728fc617765e69947b3e5@group.calendar.google.com'  # Tu ID real
TZ = pytz.timezone('America/Santiago')

print("GOOGLE_SERVICE_ACCOUNT_JSON:", os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))  # Debug
credentials_dict = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))
credentials = service_account.Credentials.from_service_account_info(credentials_dict, scopes=SCOPES)

# Prompt base (mismo)
prompt_base = """
Responde como asistente empático y holístico de Eqilibrio.cl (Centro de Quiropraxia, Neurología Funcional, Kinesiología y Medicina China en Viña del Mar). Sé cercano, motivador y usa un tono que transmita equilibrio mente-cuerpo (ej: '¡Estamos aquí para equilibrar tu bienestar!'). 
Enfócate en info sobre servicios, precios, horarios, ubicación y consultas comunes. El servicio principal es el 'Método Equilibrio', que incluye en una sola sesión: evaluación, diagnóstico y tratamiento quiropráctico, complementado con kinesiología, neurología funcional, tratamiento autónomo y acupuntura, según la evaluación del quiropráctico.
Precios: Primera consulta $35.000, sesiones normales $40.000, programa de 4 sesiones $120.000. Pagos: efectivo, transferencia, Flow (idealmente anticipado para reservar).
Ubicación: Avenida Reñaca Norte 25, Oficina 1506, Edificio Vista Montemar, Viña del Mar. No hay estacionamiento propio, pero hay pagado al lado. Puedes sugerir compartir link de Google Maps.
Para consultas complejas o sobre condiciones de salud: Di que un profesional responderá pronto.

Horarios generales: martes 15:00-19:00, miércoles 10:00-18:00, jueves 15:00-19:00, viernes 10:00-18:00, sábado 10:00-13:00; sesiones de 1 hora; lunes cerrado.

Si la pregunta es sobre agendar una cita, analiza si proporciona detalles (nombre, teléfono/email, fecha preferida en YYYY-MM-DD, hora en HH:MM).
- Si tiene todos los detalles para agendar (nombre, contacto, fecha, hora), responde SOLO con JSON: {"intent": "schedule", "name": "nombre", "contact": "teléfono o email", "date": "YYYY-MM-DD", "time": "HH:MM"}
- Si faltan detalles, responde SOLO con JSON: {"intent": "schedule", "missing": ["lista de faltantes, ej: name, contact, date, time"]}
- Para otras preguntas, responde normalmente con texto amigable, usando Markdown donde aplique.

Responde inteligentemente a preguntas naturales. Usa Markdown: *negrita* para highlights, - listas para precios/horarios/beneficios.
Ejemplos: Si preguntan precios, lista opciones. Si "ubicación", envía detalles. Si "qué incluye la sesión", explica el Método Equilibrio.
Si no entiendes o es complejo, di: 'Voy a consultar con el equipo para darte la mejor respuesta. ¡Mientras, cuéntame más sobre tu consulta!'.
"""

# Estado simple: dict con user_phone -> {'last_time': timestamp, 'messages': []}
SESSIONS = defaultdict(lambda: {'last_time': 0, 'messages': []})
TIMEOUT = 30  # Segundos para agrupar mensajes

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    incoming_msg = request.values.get('Body', '').strip()
    from_phone = request.values.get('From', '')  # Número del usuario
    resp = MessagingResponse()
    msg = resp.message()

    current_time = time.time()
    session = SESSIONS[from_phone]

    # Agrupa mensajes si dentro de timeout
    if current_time - session['last_time'] < TIMEOUT:
        session['messages'].append(incoming_msg)
    else:
        session['messages'] = [incoming_msg]
    session['last_time'] = current_time

    # Si pasó timeout o es último msg, procesa todo agrupado
    if current_time - session['last_time'] >= TIMEOUT:  # Espera para agrupar, pero para testing responde siempre
        grouped_msg = ' '.join(session['messages'])
        ai_prompt = prompt_base + f"\nPregunta del usuario: {grouped_msg}"
        ai_response = generate_ai_response(ai_prompt)

        # Intenta parsear como JSON
        try:
            # Strip para limpiar
            cleaned_response = ai_response.strip().strip('`').strip('json') if ai_response else ''
            data = json.loads(cleaned_response)
            if data.get('intent') == 'schedule':
                if 'missing' in data:
                    missing = ', '.join(data['missing'])
                    msg.body(f"¡Genial, quieres agendar una cita! Pero necesito más detalles: {missing}. ¿Me los proporcionas?")
                else:
                    # Extrae y agenda (mismo código)
                    name = data.get('name')
                    contact = data.get('contact')
                    date_str = data.get('date')
                    time_str = data.get('time')
                    try:
                        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                        dt = TZ.localize(dt)
                        end_dt = dt + datetime.timedelta(hours=1)
                        weekday = dt.weekday()
                        hour = dt.hour
                        if weekday == 0:
                            raise ValueError("Cerrado los lunes.")
                        elif weekday == 1 or weekday == 3:
                            if not (15 <= hour < 19):
                                raise ValueError("Fuera de horario para este día (15:00-19:00).")
                        elif weekday == 2 or weekday == 4:
                            if not (10 <= hour < 18):
                                raise ValueValue("Fuera de horario para este día (10:00-18:00).")
                        elif weekday == 5:
                            if not (10 <= hour < 13):
                                raise ValueError("Fuera de horario para este día (10:00-13:00).")
                        else:
                            raise ValueError("Cerrado los domingos.")
                        busy = check_freebusy(dt, end_dt)
                        if busy:
                            msg.body("Lo siento, esa hora ya está ocupada. ¿Pruebas otra?")
                        else:
                            create_appointment(name, contact, dt)
                            msg.body(f"¡Cita agendada para {name} el {date_str} a las {time_str}! Te enviaremos confirmación y recordatorio. ¡Cuida tu equilibrio!")
                    except Exception as e:
                        msg.body(f"Error al agendar: {str(e)}. Intenta de nuevo o contacta al equipo.")
                session['messages'] = []  # Limpia sesión
                return str(resp)
        except json.JSONDecodeError:
            # Si falla parse pero parece JSON, maneja como error
            if '{' in ai_response and '}' in ai_response:
                msg.body("Lo siento, hubo un error interno. Intenta de nuevo.")
            else:
                msg.body(ai_response)
        session['messages'] = []  # Limpia

    # Si no responde aún (agrupando), retorna vacío (no responde hasta agrupar)
    return str(resp) if 'msg' in locals() else ''

# Funciones generate_ai_response, check_freebusy, create_appointment (mismas)

if __name__ == '__main__':
    app.run(port=5000)