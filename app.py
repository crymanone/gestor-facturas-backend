# app.py - VERSIÓN CON ESTADO Y NOTAS
import os
import json
import io
from flask import Flask, request, jsonify, g
from functools import wraps
from PIL import Image
import google.generativeai as genai
import database as db
from pypdf import PdfReader
import firebase_admin
from firebase_admin import credentials, auth

app = Flask(__name__)

# ... (inicialización de Firebase y Gemini sin cambios) ...

with app.app_context():
    db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
# ... (función check_token sin cambios) ...

prompt_plantilla_factura = """
🔥🔥🔥 IMPORTANTE: EXTRACCIÓN DE CONCEPTOS OBLIGATORIA 🔥🔥🔥
Eres un experto contable analizando una factura. DEBES extraer los conceptos SIEMPRE.
INSTRUCCIONES ESPECÍFICAS PARA CONCEPTOS:
1. BUSCA en la factura: tablas, listas, líneas con productos/servicios.
2. SI hay conceptos detallados: extrae CADA UNO con descripción, cantidad y precio.
3. SI NO hay conceptos detallados: crea UN concepto general con:
   - descripcion: "Varios productos/servicios" + breve descripción
   - cantidad: 1.0
   - precio_unitario: el total de la factura
🔥🔥🔥 NUEVO: ANÁLISIS DE ESTADO 🔥🔥🔥
4. BUSCA EVIDENCIA VISUAL de que la factura ha sido pagada. Esto incluye sellos de "PAGADO", "COBRADO", "CANCELADO", texto manuscrito que indique pago, o una firma en la zona de totales.
5. Si encuentras dicha evidencia, establece el campo "estado" a "Pagada". De lo contrario, déjalo como null.

FORMATO JSON OBLIGATORIO:
{
  "emisor": "nombre", "cif": "identificador", "fecha": "DD/MM/AAAA", "total": 100.0,
  "base_imponible": 82.64, "impuestos": {"iva": 21.0}, "estado": "Pagada",
  "conceptos": [{"descripcion": "Producto 1", "cantidad": 2.0, "precio_unitario": 25.0}]
}
NUNCA devuelvas un array vacío en "conceptos". SIEMPRE debe haber al menos 1 concepto.
"""

prompt_multipagina_pdf = """
Actúa como un experto contable. Te proporciono una serie de textos e imágenes extraídos de las páginas de UNA ÚNICA factura en PDF.
Analiza todo el contenido en conjunto para obtener una respuesta final y unificada.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos, y estado.
Para el campo "estado", busca evidencia visual de pago como sellos ('PAGADO', 'COBRADO') o firmas. Si la encuentras, pon "Pagada", si no, déjalo como null.
Si un campo aparece en varias páginas (ej. 'emisor'), usa el de la primera aparición. Si los conceptos se reparten en varias páginas, combínalos todos en una sola lista. El 'total' y la 'base_imponible' suelen estar en la última página; prioriza esos.
"""
# ... (endpoints /api/process_invoice, /api/upload_pdf, /api/job_status, /api/process_queue sin cambios) ...

# ... (endpoint /api/invoices, método POST, necesita una pequeña modificación) ...
@app.route('/api/invoices', methods=['GET', 'POST'])
@check_token
def handle_invoices():
    if request.method == 'GET':
        # ... (sin cambios)
    if request.method == 'POST':
        try:
            invoice_data = request.get_json()
            if not invoice_data or not invoice_data.get('emisor'):
                return jsonify({"ok": False, 'error': "Datos inválidos o emisor faltante"}), 400
            
            # Asegurarse de que el campo notas exista para la inserción manual
            if 'notas' not in invoice_data:
                invoice_data['notas'] = None

            new_id = db.add_invoice(invoice_data, "Manual", g.user_id)
            if new_id:
                return jsonify({"ok": True, "id": new_id}), 201
            else:
                return jsonify({"ok": False, "error": "No se pudo guardar la factura"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

# --- NUEVO ENDPOINT PARA LAS NOTAS ---
@app.route('/api/invoice/<int:invoice_id>/notes', methods=['PUT'])
@check_token
def update_invoice_notes(invoice_id):
    try:
        data = request.get_json()
        if 'notas' not in data:
            return jsonify({"ok": False, "error": "Falta el campo 'notas'"}), 400
        notes = data.get('notas')
        success = db.update_invoice_notes(invoice_id, g.user_id, notes)
        if success:
            return jsonify({"ok": True, "message": "Notas actualizadas correctamente"})
        else:
            return jsonify({"ok": False, "error": "Factura no encontrada o no se pudo actualizar"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/search', methods=['POST'])
@check_token
def search():
    try:
        data = request.get_json()
        text_query = data.get('text_query')
        date_from_raw = data.get('date_from')
        date_to_raw = data.get('date_to')
        
        date_from = f"{date_from_raw[6:10]}-{date_from_raw[3:5]}-{date_from_raw[0:2]}" if date_from_raw else None
        date_to = f"{date_to_raw[6:10]}-{date_to_raw[3:5]}-{date_to_raw[0:2]}" if date_to_raw else None
        
        results = db.search_invoices(g.user_id, text_query, date_from, date_to)
        return jsonify({"ok": True, "invoices": results})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/ask', methods=['POST'])
@check_token
def ask_assistant():
    try:
        query_data = request.get_json()
        if not query_data or 'query' not in query_data:
            return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400
            
        user_query = query_data['query']
        all_invoices = db.get_all_invoices_with_details(g.user_id)
        
        if not all_invoices:
            return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada todavía."})
            
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
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))