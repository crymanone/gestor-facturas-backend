# app.py - VERSIÓN FINAL CON SUPABASE

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
from import database as db # Importación relativa
import fitz

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: raise ValueError("No GOOGLE_API_KEY")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

app = Flask(__name__)
db.init_db() # Solo verifica la conexión

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

prompt_pagina_pdf = """
Actúa como un experto contable analizando una PÁGINA de una factura que podría tener varias páginas.
La imagen que te paso es una de esas páginas. NO es la factura completa.
Extrae TODOS los campos que puedas ver en ESTA PÁGINA. Los campos son:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos.
Devuelve la respuesta estrictamente en formato JSON. Si un campo no está en esta página, devuélvelo como `null`.
Es CRÍTICO que extraigas todos los conceptos ('line items') que veas en esta página.
"""

def combine_pdf_pages_data(pages_data):
    if not pages_data: return None
    final_invoice = {"conceptos": []}
    for page_data in pages_data:
        for key, value in page_data.items():
            if key == "conceptos" and isinstance(value, list):
                final_invoice["conceptos"].extend(value)
            elif final_invoice.get(key) is None and value is not None:
                final_invoice[key] = value
            elif key in ['total', 'base_imponible', 'fecha'] and value is not None:
                final_invoice[key] = value
    return final_invoice

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
        invoice_id = db.add_invoice(extracted_data, "gemini-1.5-flash-latest (image)")
        if invoice_id:
            return jsonify({"ok": True, "id": invoice_id})
        else:
            return jsonify({"ok": False, "error": "Los datos de la imagen no se pudieron guardar."}), 500
    except Exception as e:
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

        pdf_pages_data = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("jpeg")
            img = Image.open(io.BytesIO(img_bytes))
            response = gemini_model.generate_content([prompt_pagina_pdf, img])
            json_text = response.text.replace('```json', '').replace('```', '').strip()
            extracted_data = json.loads(json_text)
            pdf_pages_data.append(extracted_data)

        final_invoice_data = combine_pdf_pages_data(pdf_pages_data)
        if not final_invoice_data:
            return jsonify({"ok": False, "error": "No se pudo extraer ningún dato del PDF."}), 500

        invoice_id = db.add_invoice(final_invoice_data, "gemini-1.5-flash-latest (pdf)")
        if invoice_id:
            return jsonify({"ok": True, "id": invoice_id})
        else:
            return jsonify({"ok": False, "error": "Los datos del PDF no se pudieron guardar."}), 500
    except Exception as e:
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
                return jsonify({"ok": True, "id": new_id, "message": "Factura añadida"}), 201
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
        invoices_context = json.dumps(all_invoices, indent=2, ensure_ascii=False, default=str) # Añadido default=str por si hay fechas
        prompt_contextual = f"""
        Actúa como un asistente experto en contabilidad y finanzas personales...
        PREGUNTA DEL USUARIO: "{user_query}"
        ...
        """
        response = gemini_model.generate_content(prompt_contextual)
        return jsonify({"ok": True, "answer": response.text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500