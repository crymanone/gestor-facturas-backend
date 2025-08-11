# app.py - VERSIÓN COMPLETA CON ASISTENTE DE IA

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
import database as db
import speech_recognition as sr

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: raise ValueError("No se encontró GOOGLE_API_KEY.")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

app = Flask(__name__)
db.DATABASE_FILE = '/tmp/facturas.db' # Ruta para Vercel
db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

prompt_plantilla_factura = """
# ... tu prompt de extracción de facturas va aquí ...
"""

@app.route('/api/process_invoice', methods=['POST'])
def process_invoice():
    # ... (sin cambios)
@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    # ... (sin cambios)
@app.route('/api/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    # ... (sin cambios)

# >>> NUEVO "SÚPER ENDPOINT" PARA EL ASISTENTE <<<
@app.route('/api/assistant', methods=['POST'])
def assistant_query():
    if not request.data: return jsonify({"ok": False, "respuesta_hablada": "No he recibido tu pregunta."})

    r = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(request.data)) as source:
        audio_data = r.record(source)
    try:
        text = r.recognize_google(audio_data, language='es-ES')
    except Exception as e:
        return jsonify({"ok": False, "respuesta_hablada": "No he podido entender lo que has dicho."})

    prompt_router = f"""
    Eres el cerebro de una app de gestión de facturas. El usuario ha dicho: "{text}".
    Clasifica su intención y extrae las entidades.
    Intenciones: 'buscar_facturas', 'resumen_mensual', 'predecir_gastos', 'pregunta_general'.
    Entidades: 'emisor' (string), 'mes' (número del 1 al 12).
    Responde SÓLO con JSON.
    Ejemplos:
    - "Busca facturas de Vodafone" -> {{"intencion": "buscar_facturas", "entidades": {{"emisor": "Vodafone"}}}}
    - "Gastos de julio" -> {{"intencion": "buscar_facturas", "entidades": {{"mes": "7"}}}}
    - "Resumen mensual" -> {{"intencion": "resumen_mensual", "entidades": {{}}}}
    - "Crees que gastaré más el próximo mes" -> {{"intencion": "predecir_gastos", "entidades": {{}}}}
    - "Cuál es el NIF de Apple" -> {{"intencion": "pregunta_general", "entidades": {{}}}}
    """
    try:
        response = gemini_model.generate_content(prompt_router)
        result = json.loads(response.text.strip().replace("'", '"'))
        intent = result.get("intencion")
        entities = result.get("entidades", {})
        
        respuesta_hablada = "No he entendido bien, ¿puedes repetirlo?"
        datos = None

        if intent == 'buscar_facturas':
            datos = db.search_invoices(entities)
            respuesta_hablada = f"He encontrado {len(datos)} facturas que coinciden."
            if not datos: respuesta_hablada = "No he encontrado facturas con esos criterios."

        elif intent == 'resumen_mensual':
            datos = db.get_monthly_summary()
            respuesta_hablada = "Aquí está el resumen de tus gastos en los últimos meses."
        
        elif intent == 'predecir_gastos':
            summary_data = db.get_monthly_summary()
            if not summary_data:
                respuesta_hablada = "No tengo suficientes datos para hacer una predicción."
            else:
                prompt_prediccion = f"Basado en estos datos de gastos (mes:gasto): {json.dumps(summary_data)}. Estima el gasto del próximo mes. Justifica tu respuesta en una o dos frases amigables."
                prediction_response = gemini_model.generate_content(prompt_prediccion)
                respuesta_hablada = prediction_response.text
            
        elif intent == 'pregunta_general':
             respuesta_hablada = gemini_model.generate_content(text).text

        return jsonify({"ok": True, "respuesta_hablada": respuesta_hablada, "datos": datos, "intencion": intent})
        
    except Exception as e:
        return jsonify({"ok": False, "respuesta_hablada": "Ha ocurrido un error en mi cerebro de IA. Inténtalo de nuevo."})