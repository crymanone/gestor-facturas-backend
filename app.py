# app.py - VERSIÓN COMPLETA PARA DESPLIEGUE EN VERCEL

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
    if not api_key:
        raise ValueError("No se encontró la GOOGLE_API_KEY en las variables de entorno.")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

app = Flask(__name__)
# Vercel tiene un sistema de archivos de solo lectura, excepto /tmp
# Inicializamos la BBDD en /tmp para que sea escribible
db.DATABASE_FILE = '/tmp/facturas.db'
db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

prompt_plantilla_factura = """
Actúa como un experto contable especializado en la extracción de datos de documentos.
Analiza la siguiente imagen de una factura o ticket.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON, sin texto introductorio, explicaciones o marcado de código.

Los campos a extraer son:
- emisor (El nombre de la empresa o persona que emite la factura)
- cif (El identificador fiscal: CIF, NIF, VAT ID, etc.)
- fecha (La fecha de emisión del documento en formato DD/MM/AAAA)
- total (El importe total final pagado, como un número)
- base_imponible (El subtotal antes de impuestos, como un número)
- impuestos (Un objeto JSON con los diferentes tipos de impuesto y su valor. Ej: {"iva_21": 21.00, "otros_impuestos": 2.50})
- conceptos (Una lista de objetos, donde cada objeto contiene 'descripcion', 'cantidad' y 'precio_unitario')

Si un campo no se puede encontrar o no es aplicable, devuélvelo como `null`.
Si los conceptos son difíciles de desglosar, extrae al menos una descripción general como un único concepto.
"""

@app.route('/api/process_invoice', methods=['POST'])
def process_invoice():
    if not request.data: return jsonify({"error": "No se ha enviado ninguna imagen"}), 400
    try:
        image_bytes = io.BytesIO(request.data); img = Image.open(image_bytes)
        response = gemini_model.generate_content([prompt_plantilla_factura, img])
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        extracted_data = json.loads(json_text)
        db.add_invoice(extracted_data, "gemini-1.5-flash-latest (real)")
        return jsonify({"ok": True, "data": extracted_data})
    except Exception as e:
        return jsonify({"error": f"Error del servidor de IA: {e}"}), 500

@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    try:
        sort_by = request.args.get('sort_by', 'fecha')
        order = request.args.get('order', 'DESC')
        invoices = db.get_all_invoices(sort_by=sort_by, order=order)
        return jsonify({"ok": True, "invoices": invoices})
    except Exception as e:
        return jsonify({"error": f"Error del servidor: {e}"}), 500

@app.route('/api/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    try:
        details = db.get_invoice_details(invoice_id)
        if details: return jsonify({"ok": True, "invoice": details})
        else: return jsonify({"ok": False, "error": "Factura no encontrada"}), 404
    except Exception as e:
        return jsonify({"error": f"Error del servidor: {e}"}), 500

@app.route('/api/voice_command', methods=['POST'])
def voice_command():
    if not request.data:
        return jsonify({"ok": False, "error": "No se recibió audio"}), 400
    
    r = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(request.data)) as source:
        audio_data = r.record(source)
    try:
        text = r.recognize_google(audio_data, language='es-ES')
        prompt_intencion = f"""
        Analiza el siguiente comando: '{text}'. Determina la intención del usuario ('show_list', 'show_capture_screen', 'unknown').
        Responde SÓLO con JSON: {{"intent": "valor"}}
        """
        response = gemini_model.generate_content(prompt_intencion)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        intent_data = json.loads(json_text)
        return jsonify({"ok": True, "text": text, "intent": intent_data.get('intent', 'unknown')})
    except sr.UnknownValueError:
        return jsonify({"ok": False, "error": "Audio ininteligible"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)