# app.py - VERSIÓN FINAL CON ENDPOINT DE BORRADO

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
import database as db
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

prompt_multipagina_pdf = """
Actúa como un experto contable. Te proporciono una serie de imágenes que corresponden a las páginas de UNA ÚNICA factura en PDF.
Analiza todas las páginas en conjunto para obtener una respuesta final y unificada.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos.
Si un campo aparece en varias páginas (ej. 'emisor'), usa el de la primera aparición. Si los conceptos se reparten en varias páginas, combínalos todos en una sola lista. El 'total' y la 'base_imponible' suelen estar en la última página; prioriza esos.
Si un campo no se puede encontrar en ninguna página, devuélvelo como `null`.
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
        invoice_id = db.add_invoice(extracted_data, "gemini-1.5-flash (image)")
        if invoice_id:
            return jsonify({"ok": True, "id": invoice_id})
        else:
            return jsonify({"ok": False, "error": "Los datos de la imagen no se pudieron guardar."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500

@app.route('/api/upload_pdf', methods=['POST'])
def upload_pdf():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ningún fichero PDF"}), 400
    pdf_bytes = request.data
    job_id = db.create_pdf_job(pdf_bytes)
    if job_id:
        return jsonify({"ok": True, "job_id": job_id})
    else:
        return jsonify({"ok": False, "error": "No se pudo crear el trabajo de procesamiento."}), 500

@app.route('/api/job_status/<job_id>', methods=['GET'])
def job_status(job_id):
    status = db.get_job_status(job_id)
    if status:
        return jsonify({"ok": True, "status": status})
    else:
        return jsonify({"ok": False, "error": "Job ID no encontrado."}), 404

@app.route('/api/process_queue', methods=['GET'])
def process_queue():
    job = db.get_pending_pdf_job()
    if not job:
        return "No pending jobs found.", 200
    job_id = job['id']
    pdf_bytes = job['pdf_data']
    try:
        doc = fitz.open(stream=bytes(pdf_bytes), filetype="pdf")
        if not len(doc):
            raise ValueError("El PDF en la cola está vacío.")
        image_parts = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes_page = pix.tobytes("jpeg")
            img = Image.open(io.BytesIO(img_bytes_page))
            image_parts.append(img)
        response = gemini_model.generate_content([prompt_multipagina_pdf] + image_parts)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        final_invoice_data = json.loads(json_text)
        invoice_id = db.add_invoice(final_invoice_data, "gemini-1.5-flash (pdf)")
        if not invoice_id:
            raise ValueError("Falló el guardado en la tabla de facturas.")
        db.update_job_as_completed(job_id, final_invoice_data)
        return f"Job {job_id} processed successfully.", 200
    except Exception as e:
        print(f"Error processing job {job_id}: {e}")
        db.update_job_as_failed(job_id, str(e))
        return f"Failed to process job {job_id}.", 500

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

@app.route('/api/invoice/<int:invoice_id>', methods=['GET', 'DELETE'])
def handle_single_invoice(invoice_id):
    if request.method == 'GET':
        try:
            details = db.get_invoice_details(invoice_id)
            if details:
                return jsonify({"ok": True, "invoice": details})
            else:
                return jsonify({"ok": False, "error": "Factura no encontrada"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error del servidor: {e}"}), 500
    if request.method == 'DELETE':
        try:
            success = db.delete_invoice(invoice_id)
            if success:
                return jsonify({"ok": True, "message": "Factura borrada correctamente"})
            else:
                return jsonify({"ok": False, "error": "No se pudo borrar la factura"}), 500
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