# app.py - VERSIÓN FINAL CON PROMPTS MEJORADOS Y ROBUSTOS

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
import database as db # Asegúrate de que esta línea esté así
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

# --- >>> PROMPT MEJORADO CON EJEMPLOS Y DIRECTIVAS MÁS CLARAS <<< ---
prompt_plantilla_factura = """
Actúa como un experto contable y extractor de datos.
Analiza la imagen de la factura o ticket. Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON.

REGLAS IMPORTANTES:
1.  Si un campo no se encuentra, devuélvelo como `null`, NO como una cadena vacía.
2.  Para 'total' y 'base_imponible', busca siempre el valor final y más alto. Ignora subtotales. Deben ser números, no texto.
3.  Para 'conceptos', extrae cada línea de producto o servicio como un objeto separado en la lista. Si es imposible desglosar, crea un único concepto con una descripción general.

EJEMPLO DE RESPUESTA JSON:
{
  "emisor": "Mercadona S.A.",
  "cif": "A-12345678",
  "fecha": "14/08/2025",
  "total": 15.75,
  "base_imponible": 14.50,
  "impuestos": {
    "iva_10": 1.25
  },
  "conceptos": [
    {
      "descripcion": "PAN HOGAZA",
      "cantidad": 1,
      "precio_unitario": 1.50
    },
    {
      "descripcion": "LECHE SEMI",
      "cantidad": 6,
      "precio_unitario": 0.90
    }
  ]
}
"""

# --- >>> PROMPT DE PDF TAMBIÉN MEJORADO <<< ---
prompt_multipagina_pdf = """
Actúa como un experto contable. Te proporciono una serie de imágenes que son las páginas de UNA ÚNICA factura.
Analiza TODAS las páginas en conjunto para obtener una respuesta final y unificada.

REGLAS IMPORTANTES:
1.  Busca el 'emisor', 'cif' y 'fecha' principalmente en la primera página.
2.  Busca el 'total' y la 'base_imponible' principalmente en la última página.
3.  ACUMULA todos los 'conceptos' de todas las páginas en una única lista en el resultado final.
4.  Si un campo no se encuentra en NINGUNA página, devuélvelo como `null`.
5.  La respuesta final debe ser un ÚNICO objeto JSON.

Devuelve un solo objeto JSON con la información combinada.
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
            img_bytes_page = pix.tobytes("jpeg")
            img = Image.open(io.BytesIO(img_bytes_page))
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
        Actúa como un asistente experto en contabilidad y finanzas personales.
        A continuación, te proporciono una lista de las facturas de un usuario en formato JSON.
        Tu tarea es responder a la pregunta del usuario basándote únicamente en estos datos.

        DATOS DE LAS FACTURAS:
        ```json
        {invoices_context}
        ```

        PREGUNTA DEL USUARIO:
        "{user_query}"

        Proporciona una respuesta clara, concisa y directa. Si la pregunta no se puede responder con los datos proporcionados, indica amablemente que no tienes esa información. No inventes datos.
        """
        response = gemini_model.generate_content(prompt_contextual)
        return jsonify({"ok": True, "answer": response.text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500