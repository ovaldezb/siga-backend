import base64
import tempfile
import unicodedata
from fpdf import FPDF
import xml.etree.ElementTree as ET
import io
import os
from num2words import num2words
from PIL import Image

# fpdf 1.7.2 con fuentes core (Arial) solo sabe codificar latin-1: cualquier
# caracter fuera de ese rango lanza UnicodeEncodeError y tumbaba la generacion
# del PDF. La enie y los acentos si caben en latin-1, pero la comilla tipografica,
# el guion largo o el simbolo de grados no, y llegan seguido desde el autollenado
# de la Constancia de Situacion Fiscal.
_TRANSLITERACIONES = {
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '‘': "'", '’': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"',
    '′': "'", '″': '"',
    '–': '-', '—': '-', '―': '-', '−': '-',
    '…': '...', '•': '-', ' ': ' ',
    '€': 'EUR', '™': '(TM)', '℃': ' C', '℉': ' F',
}


def sanea_latin1(texto):
    """Deja el texto en un subconjunto que fpdf pueda imprimir sin reventar.

    Degrada caracter por caracter (no con un NFKD global) para no perder la
    enie ni los acentos, que si son representables en latin-1.
    """
    if texto is None:
        return ''
    if not isinstance(texto, str):
        texto = str(texto)
    for original, reemplazo in _TRANSLITERACIONES.items():
        texto = texto.replace(original, reemplazo)
    try:
        texto.encode('latin-1')
        return texto
    except UnicodeEncodeError:
        pass
    salida = []
    for caracter in texto:
        try:
            caracter.encode('latin-1')
            salida.append(caracter)
        except UnicodeEncodeError:
            degradado = unicodedata.normalize('NFKD', caracter).encode('latin-1', 'ignore').decode('latin-1')
            salida.append(degradado if degradado else '?')
    return ''.join(salida)


def safe_float(val, default=0.0):
    try:
        return float(val) if val else default
    except Exception:
        return default


class _FPDFSeguro(FPDF):
    """FPDF que sanea todo texto antes de escribirlo."""

    def cell(self, w, h=0, txt='', *args, **kwargs):
        return super().cell(w, h, sanea_latin1(txt), *args, **kwargs)

    def multi_cell(self, w, h, txt='', *args, **kwargs):
        return super().multi_cell(w, h, sanea_latin1(txt), *args, **kwargs)


class CFDIPDF_FPDF_Generator():
    def __init__(self, xml_string: str, qrCode: str, cadena_original_sat: str, noTicket: str, fecha_hora_venta: str, direccion: str, empresa: str, regimen_fiscal_emisor: str, regimen_fiscal_receptor: str, logo_path: str = None) -> None:
        self.xml_string = xml_string
        self.qrCode = qrCode
        self.cadena_original_sat = cadena_original_sat
        self.noTicket = noTicket
        self.fecha_hora_venta = fecha_hora_venta
        self.direccion = direccion
        self.empresa = empresa
        self.regimen_fiscal_emisor = regimen_fiscal_emisor
        self.regimen_fiscal_receptor = regimen_fiscal_receptor
        self.logo_path = logo_path
        self.root = ET.fromstring(xml_string)
        self.data = self._parse_cfdi()

    def _parse_cfdi(self):
        ns = {'cfdi': 'http://www.sat.gob.mx/cfd/4'}
        data = {}
        comprobante = self.root
        data['serie'] = comprobante.attrib.get('Serie', '')
        data['folio'] = comprobante.attrib.get('Folio', '')
        data['tipo_cambio'] = comprobante.attrib.get('TipoCambio', '')
        data['lugar_expedicion'] = comprobante.attrib.get('LugarExpedicion', '')
        data['tipo_comprobante'] = comprobante.attrib.get('TipoDeComprobante', '')
        data['fecha'] = comprobante.attrib.get('Fecha', '')
        data['total'] = comprobante.attrib.get('Total', '')
        data['subtotal'] = comprobante.attrib.get('SubTotal', '')
        data['metodo_pago'] = comprobante.attrib.get('MetodoPago', '')
        data['forma_pago'] = comprobante.attrib.get('FormaPago', '')
        data['moneda'] = comprobante.attrib.get('Moneda', '')
        data['emisor'] = comprobante.find('cfdi:Emisor', ns).attrib if comprobante.find('cfdi:Emisor', ns) is not None else {}
        data['receptor'] = comprobante.find('cfdi:Receptor', ns).attrib if comprobante.find('cfdi:Receptor', ns) is not None else {}
        conceptos = comprobante.find('cfdi:Conceptos', ns)
        data['conceptos'] = []
        if conceptos is not None:
            for concepto in conceptos.findall('cfdi:Concepto', ns):
                concepto_data = concepto.attrib.copy()
                traslados = None
                traslado = None
                impuestos = concepto.find('cfdi:Impuestos', ns)
                if impuestos is not None:
                    traslados = impuestos.find('cfdi:Traslados', ns)
                    if traslados is not None:
                        traslado = traslados.find('cfdi:Traslado', ns)
                concepto_data['impuestos'] = traslado.attrib if traslado is not None else {}
                data['conceptos'].append(concepto_data)
        timbre = comprobante.find('cfdi:Complemento/tfd:TimbreFiscalDigital', 
                                  {
                                      'cfdi': 'http://www.sat.gob.mx/cfd/4',
                                      'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'
                                  }
                                  )
        data['uuid'] = timbre.attrib.get('UUID', '') if timbre is not None else ''
        data['sello_cfdi'] = timbre.attrib.get('SelloCFD', '') if timbre is not None else ''
        data['sello_sat'] = timbre.attrib.get('SelloSAT', '') if timbre is not None else ''
        data['fecha_timbrado'] = timbre.attrib.get('FechaTimbrado', '') if timbre is not None else ''
        data['NoCertificadoSAT'] = timbre.attrib.get('NoCertificadoSAT', '') if timbre is not None else ''
        return data

    def generate_pdf(self) -> bytes:
        pdf = _FPDFSeguro()
        pdf.add_page()
        pdf.set_auto_page_break(auto=False, margin=10)
        
        # 1. Franja superior
        pdf.set_font("Arial", '', 6)
        pdf.cell(0, 5, "Este documento es una representación impresa de un CFDI", ln=True, align='L')
        pdf.set_line_width(0.4)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(2)

        # 2. Encabezado en 3 columnas independientes (Logo, Emisor, CFDI)
        y_top = pdf.get_y()
        
        # --- Columna 1: Logo ---
        logo_w, logo_h = 0.0, 0.0
        if self.logo_path and os.path.exists(self.logo_path):
            try:
                with Image.open(self.logo_path) as img:
                    orig_w, orig_h = img.size
                max_w, max_h = 32.0, 18.0
                ratio = min(max_w / orig_w, max_h / orig_h)
                logo_w = orig_w * ratio
                logo_h = orig_h * ratio
                pdf.image(self.logo_path, x=10, y=y_top, w=logo_w, h=logo_h)
            except Exception as e:
                pdf.set_xy(10, y_top)
                pdf.set_font("Arial", 'B', 9)
                pdf.multi_cell(32, 4, self.empresa or self.data['emisor'].get('Nombre', ''))
                logo_h = pdf.get_y() - y_top
        else:
            pdf.set_xy(10, y_top)
            pdf.set_font("Arial", 'B', 9)
            pdf.multi_cell(32, 4, self.empresa or self.data['emisor'].get('Nombre', ''))
            logo_h = pdf.get_y() - y_top

        # --- Columna 2: Datos del Emisor ---
        x_emisor = 45
        w_emisor = 68
        pdf.set_xy(x_emisor, y_top)
        
        emisor = self.data['emisor']
        nombre_emisor = emisor.get('Nombre', '') or self.empresa or ''
        pdf.set_font("Arial", 'B', 8.5)
        pdf.cell(w_emisor, 4.0, nombre_emisor, ln=True)
        
        pdf.set_x(x_emisor)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(w_emisor, 3.8, emisor.get('Rfc', ''), ln=True)
        
        if self.direccion:
            pdf.set_x(x_emisor)
            pdf.set_font("Arial", '', 7.0)
            pdf.multi_cell(w_emisor, 3.2, self.direccion)
            
        regimen_emisor = self.regimen_fiscal_emisor or emisor.get('RegimenFiscal', '')
        if regimen_emisor:
            pdf.set_x(x_emisor)
            pdf.set_font("Arial", '', 7.0)
            reg_text = f"Régimen Fiscal: {regimen_emisor}"
            pdf.multi_cell(w_emisor, 3.2, reg_text)
            
        y_emisor_end = pdf.get_y()

        # --- Columna 3: Datos CFDI / Factura ---
        x_cfdi = 116
        w_cfdi_lbl = 28
        w_cfdi_val = 56
        
        fecha_clean = self.fecha_hora_venta or self.data.get('fecha', '')
        if 'T' in fecha_clean and len(fecha_clean) > 19:
            fecha_clean = fecha_clean[:19].replace('T', ' ')
        elif 'T' in fecha_clean:
            fecha_clean = fecha_clean.replace('T', ' ')
            
        tipo_comp = self.data.get('tipo_comprobante', 'I')
        tipo_map = {'I': 'I - Ingreso', 'E': 'E - Egreso', 'T': 'T - Traslado', 'P': 'P - Pago', 'N': 'N - Nómina'}
        tipo_comp_str = tipo_map.get(tipo_comp, tipo_comp)
        
        cfdi_rows = [
            ("Folio Fiscal:", self.data.get('uuid', '')),
            ("Serie y Folio:", f"{self.data.get('serie', '')} {self.data.get('folio', '')}".strip()),
            ("Fecha y Hora:", fecha_clean),
            ("Tipo de Comprobante:", tipo_comp_str),
            ("Lugar Expedición:", self.data.get('lugar_expedicion', ''))
        ]
        
        y_cfdi = y_top
        for lbl, val in cfdi_rows:
            pdf.set_xy(x_cfdi, y_cfdi)
            pdf.set_font("Arial", 'B', 7.5)
            pdf.cell(w_cfdi_lbl, 3.8, lbl, align='R')
            pdf.set_font("Arial", '', 7.0)
            pdf.cell(w_cfdi_val, 3.8, val, align='L')
            y_cfdi += 3.8
            
        y_cfdi_end = y_cfdi

        # Altura final del encabezado y línea separadora
        y_header_end = max(y_top + logo_h, y_emisor_end, y_cfdi_end) + 2.5
        pdf.set_line_width(0.3)
        pdf.line(10, y_header_end, 200, y_header_end)
        
        # 3. Sección Receptor / Forma de Pago
        y_rec = y_header_end + 2
        pdf.set_xy(10, y_rec)
        
        receptor = self.data['receptor']
        regimen_rec = self.regimen_fiscal_receptor or receptor.get('RegimenFiscalReceptor', '')
        
        receptor_pares = [
            ("Cliente:", receptor.get('Nombre', '')),
            ("RFC:", receptor.get('Rfc', '')),
            ("Uso CFDI:", receptor.get('UsoCFDI', '')),
            ("Domicilio Fiscal:", receptor.get('DomicilioFiscalReceptor', '')),
            ("Régimen Fiscal:", regimen_rec)
        ]

        tipo_cambio_str = str(self.data.get('tipo_cambio', '1'))
        formapago_pares = [
            ("Forma de Pago:", self.data.get('forma_pago', '')),
            ("Moneda:", self.data.get('moneda', 'MXN')),
            ("Tipo Cambio:", tipo_cambio_str),
            ("Método de Pago:", self.data.get('metodo_pago', '')),
            ("Lugar de Expedición:", self.data.get('lugar_expedicion', '')),
        ]

        x_rec = 10
        x_pago = 110
        w_rec_title = 24
        w_rec_value = 72
        w_pago_title = 32
        w_pago_value = 58
        line_height = 3.8

        max_rows = max(len(receptor_pares), len(formapago_pares))
        for i in range(max_rows):
            y_actual = pdf.get_y()
            if i < len(receptor_pares):
                titulo, valor = receptor_pares[i]
                pdf.set_xy(x_rec, y_actual)
                pdf.set_font("Arial", 'B', 7.5)
                pdf.cell(w_rec_title, line_height, titulo, ln=False)
                pdf.set_font("Arial", '', 7.5)
                pdf.cell(w_rec_value, line_height, valor, ln=False)
            else:
                pdf.set_xy(x_rec, y_actual)
                pdf.cell(w_rec_title + w_rec_value, line_height, "", ln=False)
                
            if i < len(formapago_pares):
                titulo, valor = formapago_pares[i]
                pdf.set_xy(x_pago, y_actual)
                pdf.set_font("Arial", 'B', 7.5)
                pdf.cell(w_pago_title, line_height, titulo, border=0, align='R', ln=False)
                pdf.set_font("Arial", '', 7.5)
                pdf.cell(w_pago_value, line_height, valor, border=0, align='L', ln=True)
            else:
                pdf.set_xy(x_pago, y_actual)
                pdf.cell(w_pago_title + w_pago_value, line_height, "", ln=True)
                
        pdf.ln(2)
        
        # 4. Tabla de conceptos
        pdf.set_font("Arial", 'B', 7.5)
        pdf.cell(24, 5.5, "Clave Prod/Serv", border=1, align='C')
        pdf.cell(14, 5.5, "Cantidad", border=1, align='C')
        pdf.cell(20, 5.5, "Clave Unidad", border=1, align='C')
        pdf.cell(12, 5.5, "Unidad", border=1, align='C')
        pdf.cell(60, 5.5, "Descripción", border=1, align='C')
        pdf.cell(20, 5.5, "Prec Unitario", border=1, align='C')
        pdf.cell(20, 5.5, "Impuesto", border=1, align='C')
        pdf.cell(20, 5.5, "Importe", border=1, align='C', ln=True)
        pdf.set_font("Arial", '', 7)
        impuesto_total = 0.0

        for concepto in self.data['conceptos']:
            desc = concepto.get('Descripcion', '')
            if len(desc) > 38:
                desc = desc[:35] + '...'
            cant_float = safe_float(concepto.get('Cantidad', 0))
            cant_str = f"{cant_float:.2f}"
            pdf.cell(24, 5, concepto.get('ClaveProdServ', ''), align='C', border='L')
            pdf.cell(14, 5, cant_str, align='C', border=0)
            pdf.cell(20, 5, concepto.get('ClaveUnidad', ''), align='C', border=0)
            pdf.cell(12, 5, concepto.get('Unidad', ''), align='C', border=0)
            pdf.cell(60, 5, desc, align='L', border=0)
            pdf.cell(20, 5, '$' + f"{safe_float(concepto.get('ValorUnitario', 0.0)):,.2f}", align='C', border=0)
            importe_impuesto = safe_float(concepto['impuestos'].get('Importe', 0.0) or 0.0)
            pdf.cell(20, 5, '$' + f"{importe_impuesto:,.2f}", align='C', border=0)
            impuesto_total += importe_impuesto
            pdf.cell(20, 5, '$' + f"{safe_float(concepto.get('Importe', 0.0)):,.2f}", align='C', border='R', ln=True)

        pdf.set_font("Arial", 'B', 7)
        pdf.cell(25, 9, 'OBSERVACIONES:', border='LT')
        pdf.set_font("Arial", '', 7)
        pdf.cell(125, 9, 'Esta factura ampara el documento ' + self.noTicket, border='T')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 4.5, 'Subtotal:', border='LRTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 4.5, '$' + f"{safe_float(self.data['subtotal']):,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(150, 4.5, '', border='LR')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 4.5, 'IVA 16%:', border='LRTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 4.5, '$' + f"{impuesto_total:,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(28, 5, 'IMPORTE CON LETRA:', border='LBT')
        pdf.set_font("Arial", '', 7)
        pdf.cell(122, 5, num2words(safe_float(self.data['total']), lang='es', to='currency', currency='MXN'), border='RBT')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 5, 'Total:', border='RTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 5, '$' + f"{safe_float(self.data['total']):,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(190, 2, '', border='LR', ln=True)
        
        # Código QR y Timbre Fiscal
        x, y = 10, pdf.get_y()
        w, h = 40, 40
        pdf.set_xy(x, y)
        pdf.cell(w, h, '', border='L')
        # QR (solo URL)
        try:
            image_bytes = base64.b64decode(self.qrCode)
            with tempfile.NamedTemporaryFile(delete=True, suffix=".png") as tmp_img:
                tmp_img.write(image_bytes)
                tmp_img.flush()
                pdf.image(tmp_img.name, x=x + 1, y=y + 1, w=35)
        except Exception as qr_err:
            print(f"Error decoding/drawing QR code: {qr_err}")
        
        # Timbre Fiscal
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(75, 4, "No Serie certificado SAT: ", align='C')
        pdf.cell(75, 4, "Fecha Timbrado:", align='C', ln=True, border='R')
        pdf.set_font("Arial", '', 7)
        pdf.cell(40, 5, '')
        pdf.cell(75, 5, self.data['NoCertificadoSAT'], align='C')
        pdf.cell(75, 5, self.data['fecha_timbrado'], align='C', ln=True, border='R')
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(40, 5, '')
        pdf.cell(75, 5, 'Sello Digital del SAT:', align='C')
        pdf.cell(75, 5, 'Sello Digital del EMISOR:', border='R', align='C', ln=True)
        pdf.set_font("Arial", '', 5)
        
        x1 = 50
        x2 = 125
        y = pdf.get_y()
        w1 = 75
        w2 = 75
        pdf.set_xy(x1, y)
        pdf.multi_cell(w1, 3, self.data['sello_sat'])
        h1 = pdf.get_y() - y
        pdf.set_xy(x2, y)
        pdf.multi_cell(w2, 3, self.data['sello_cfdi'], border='R')
        h2 = pdf.get_y() - y
        pdf.set_y(y + max(h1, h2))
        pdf.cell(190, 5, '', border='LR', ln=True)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(190, 5, 'Cadena Original SAT:', ln=True, border='LR')
        pdf.set_font("Arial", '', 6)
        pdf.multi_cell(190, 3, self.cadena_original_sat, border='LR')
        pdf.cell(190, 271 - pdf.get_y(), '', border='LR', ln=True)

        # Pie de página
        pdf.set_font("Arial", '', 8)
        pdf.cell(180, 5, "Este documento ampara una representación impresa de un CFDI generado electrónicamente.", border='LTB')
        pdf.cell(10, 5, f"Página {pdf.page_no()}", border="RTB", align='R')
        pdf_output = pdf.output(dest='S').encode('latin1')
        return pdf_output
