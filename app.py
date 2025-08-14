# app.py - VERSIÓN FINAL CON PROCESAMIENTO EFICIENTE DE PDF EN UNA SOLA LLAMADA

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
from . import database as db
import fitz

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: raise ValueError("No GOOGLE_API_KEY")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

app = Flask(__name__)
db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

prompt_plantilla_factura = """
Actúa como un experto contable especializado en la extracción de datos de documentos.
Analiza la siguiente imagen de una factura o ticket.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON, sin texto introductorio, explicaciones o marcado de código.
Los campos a extraer son:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos.
Si un campo no se puede encontrar o no es aplicable, devuélvelo como `null`.
"""

prompt_multipagina_pdf = """
Actúa como un experto contable. Te proporciono una serie de imágenes que corresponden a las páginas de UNA ÚNICA factura en PDF.
Analiza todas las páginas en conjunto para extraer la información completa.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos.
Si un campo aparece en varias páginas (ej. 'emisor'), usa el de la primera aparición. Si los conceptos se reparten en varias páginas, combínalos todos en una sola lista. El 'total' y la 'base_imponible' suelen estar en la última página; prioriza esos.
Si un campo no se puede encontrar en ninguna página, devuélvelo como `null`.
"""

@app.route('/api/process_invoice', methods=['POST'])
def process_invoice():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ninguna imagen"}), 400
    try:
        image_bytes = io.BytesIO(request.data)
        img = Image.open(image_bytes)
        response = gemini_model.generate_content([prompt_plantilla_factura, img])
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        extracted_data = json.loads(json_text)
        
        print("--- INTENTANDO GUARDAR DATOS DE IMAGEN ---"); print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
        invoice_id = db.add_invoice(extracted_data, "gemini-1.5-flash (image)")
        
        if invoice_id:
            print("--- DATOS GUARDADOS CON ÉXITO ---")
            return jsonify({"ok": True, "id": invoice_id})
        else:
            print("--- FALLO AL GUARDAR EN DB ---")
            return jsonify({"ok": False, "error": "Los datos de la imagen no se pudieron guardar."}), 500
    except Exception as e:
        print(f"--- ERROR INESPERADO EN process_invoice: {e} ---")
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500

@app.route('/api/process_pdf', methods=['POST'])
def process_pdf():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ningún fichero PDF"}), 400
    try:
        pdf_bytes = request.data
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) == 0:
            return jsonify({"ok": False, "error": "El PDF está vacío."}), 400

        image_parts = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("jpeg")
            img = Image.open(io.BytesIO(img_bytes))
            image_parts.append(img)
        
        response = gemini_model.generate_content([prompt_multipagina_pdf] + image_parts)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        extracted_data = json.loads(json_text)
        
        print("--- INTENTANDO GUARDAR DATOS DE PDF ---"); print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
        invoice_id = db.add_invoice(extracted_data, "gemini-1.5-flash (pdf)")

        if invoice_id:
            print("--- DATOS GUARDADOS CON ÉXITO ---")
            return jsonify({"ok": True, "id": invoice_id})
        else:
            print("--- FALLO AL GUARDAR EN DB ---")
            return jsonify({"ok": False, "error": "Los datos del PDF no se pudieron guardar."}), 500
    except Exception as e:
        print(f"--- ERROR INESPERADO en process_pdf: {e} ---")
        return jsonify({"ok": False, "error": f"Error del servidor al procesar PDF: {e}"}), 500

@app.route('/api/invoices', methods=['GET', 'POST'])
def handle_invoices():
    if request.method == 'GET':
        try:
            invoices = db.get_all_invoices()
            return jsonify({"ok": True, "invoices": invoices})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    if request.method == 'POST':
        try:
            invoice_data = request.get_json()
            if not invoice_data or not invoice_data.get('emisor'):
                return jsonify({"ok": False, "error": "Datos inválidos o emisor faltante"}), 400
            new_id = db.add_invoice(invoice_data, "Manual")
            if new_id:
                return jsonify({"ok": True, "id": new_id}), 201
            else:
                return jsonify({"ok": False, "error": "No se pudo guardar la factura"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

@app.route('/api/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    try:
        details = db.get_invoice_details(invoice_id)
        if details:
            return jsonify({"ok": True, "invoice": details})
        else:
            return jsonify({"ok": False, "error": "Factura no encontrada"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error del servidor: {e}"}), 500

@app.route('/api/search', methods=['POST'])
def search():
    try:
        data = request.get_json()
        text_query = data.get('text_query', None)
        date_from_raw = data.get('date_from', None)
        date_to_raw = data.get('date_to', None)
        date_from = None
        if date_from_raw and len(date_from_raw) == 10:
            parts = date_from_raw.split('/')
            if len(parts) == 3: date_from = f"{parts[2]}-{parts[1]}-{parts[0]}"
        date_to = None
        if date_to_raw and len(date_to_raw) == 10:
            parts = date_to_raw.split('/')
            if len(parts) == 3: date_to = f"{parts[2]}-{parts[1]}-{parts[0]}"
        results = db.search_invoices(text_query, date_from, date_to)
        return jsonify({"ok": True, "invoices": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/ask', methods=['POST'])
def ask_assistant():
    query_data = request.get_json()
    if not query_data or 'query' not in query_data:
        return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400
    user_query = query_data['query']
    all_invoices = db.get_all_invoices_with_details()
    if not all_invoices:
        return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada todavía."})
    try:
        invoices_context = json.dumps(all_invoices, indent=2, ensure_ascii=False, default=str)
        prompt_contextual = f"""
        Actúa como un asistente experto en contabilidad y finanzas personales...
        PREGUNTA DEL USUARIO: "{user_query}"
        ...
        """
        response = gemini_model.generate_content(prompt_contextual)
        return jsonify({"ok": True, "answer": response.text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500