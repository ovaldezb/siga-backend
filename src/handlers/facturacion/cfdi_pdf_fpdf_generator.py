import base64
import tempfile
import unicodedata
from fpdf import FPDF
import xml.etree.ElementTree as ET
import io
import os
from num2words import num2words

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
        pdf.set_font("Arial", '', 6)
        pdf.cell(0, 6, "Este documento es una representación impresa de un CFDI", ln=True, align='L')
        pdf.set_line_width(0.5)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        
        # Emisor/Receptor
        emisor = self.data['emisor']
        if self.logo_path and os.path.exists(self.logo_path):
            try:
                pdf.image(self.logo_path, x=10, y=18, w=40)
            except Exception as e:
                print(f"Error drawing logo in PDF: {e}")
                pdf.set_font("Arial", 'B', 12)
                pdf.cell(42, 10, self.empresa or emisor.get('Nombre', ''))
        else:
            pdf.set_font("Arial", 'B', 12)
            pdf.cell(42, 10, self.empresa or emisor.get('Nombre', ''))
            
        pdf.set_font("Arial", '', 8)
        pdf.cell(42, 4, '')
        pdf.cell(63, 4, emisor.get('Nombre', ''))
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(30, 4, 'Folio Fiscal:', align='R')
        pdf.set_font("Arial", '', 8)
        pdf.cell(55, 4, self.data.get('uuid', ''), align='L', ln=True)

        pdf.cell(42, 4, '')
        pdf.cell(63, 4, emisor.get('Rfc', ''))
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(30, 4, 'Serie y Folio:', align='R')
        pdf.set_font("Arial", '', 8)
        pdf.cell(55, 4, f"{self.data['serie']} {self.data['folio']}", align='L', ln=True)

        pdf.cell(42, 4, '')
        pdf.set_font("Arial", '', 8)
        pdf.cell(63, 4, self.direccion)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(30, 4, 'Fecha y Hora:', align='R')
        pdf.set_font("Arial", '', 8)
        pdf.cell(55, 4, self.fecha_hora_venta, align='L', ln=True)

        pdf.cell(42, 4, '')
        pdf.set_font("Arial", 'B', 5)
        pdf.cell(63, 3, '',)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(30, 4, 'Tipo de Comprobante:', align='R')
        pdf.set_font("Arial", '', 8)
        pdf.cell(55, 4, self.data['tipo_comprobante'], align='L', ln=True)

        pdf.cell(42, 4, '')
        pdf.set_font("Arial", '', 8)
        pdf.cell(63, 4, 'Regimen Fiscal',)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(30, 4, 'Lugar Expedición:', align='R')
        pdf.set_font("Arial", '', 8)
        pdf.cell(55, 4, self.data['lugar_expedicion'], align='L', ln=True)

        pdf.cell(42, 4, '')
        pdf.set_font("Arial", '', 8)
        pdf.cell(63, 4, self.regimen_fiscal_emisor, ln=True)

        pdf.line(10, pdf.get_y(), 200, pdf.get_y())  
        pdf.ln(2)
        receptor = self.data['receptor']
        receptor_pares = [
            ("Cliente:", receptor.get('Nombre', '')),
            ("RFC:", receptor.get('Rfc', '')),
            ("Uso CFDI:", receptor.get('UsoCFDI', '')),
            ("Domicilio Fiscal:", receptor.get('DomicilioFiscalReceptor', '')),
            ("Regimen Fiscal:", self.regimen_fiscal_receptor)
        ]

        formapago_pares = [
            ("Forma de Pago:", self.data.get('forma_pago', '')),
            ("Moneda:", self.data.get('moneda', '')),
            ("Tipo Cambio:", self.data['tipo_cambio']),
            ("Método de Pago:", self.data['metodo_pago']),
            ("Lugar de Expedición:", self.data['lugar_expedicion']),
        ]

        x_cliente = 10
        x_pago = 110
        w_cliente_title = 25
        w_cliente_value = 70
        w_pago_title = 35
        w_pago_value = 55
        line_height = 4

        max_rows = max(len(receptor_pares), len(formapago_pares))
        for i in range(max_rows):
            y_actual = pdf.get_y()
            if i < len(receptor_pares):
                titulo, valor = receptor_pares[i]
                pdf.set_xy(x_cliente, y_actual)
                pdf.set_font("Arial", 'B', 8)
                pdf.cell(w_cliente_title, line_height, titulo, ln=False)
                pdf.set_font("Arial", '', 8)
                pdf.cell(w_cliente_value, line_height, valor, ln=False)
            else:
                pdf.set_xy(x_cliente, y_actual)
                pdf.cell(w_cliente_title + w_cliente_value, line_height, "", ln=False)
            if i < len(formapago_pares):
                titulo, valor = formapago_pares[i]
                pdf.set_xy(x_pago, y_actual)
                pdf.set_font("Arial", 'B', 8)
                pdf.cell(w_pago_title, line_height, titulo, border=0, align='R', ln=False)
                pdf.set_font("Arial", '', 8)
                pdf.cell(w_pago_value, line_height, valor, border=0, align='L', ln=True)
            else:
                pdf.set_xy(x_pago, y_actual)
                pdf.cell(w_pago_title + w_pago_value, line_height, "", ln=True)
        pdf.set_line_width(0.4)
        pdf.ln(3)
        
        # Tabla de conceptos
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(24, 6, "Clave Prod/Serv", border=1)
        pdf.cell(14, 6, "Cantidad", border=1)
        pdf.cell(20, 6, "Clave Unidad", border=1)
        pdf.cell(12, 6, "Unidad", border=1)
        pdf.cell(60, 6, "Descripción", border=1, align='C')
        pdf.cell(20, 6, "Prec Unitario", border=1, align='C')
        pdf.cell(20, 6, "Impuesto", border=1, align='C')
        pdf.cell(20, 6, "Importe", border=1, align='C', ln=True)
        pdf.set_font("Arial", '', 7)
        impuesto_total = 0.0
        MAX_LENGTH_DESCRIPCION = 40
        descripcion_full = ''
        last_space = 0

        for concepto in self.data['conceptos']:
            row_size = 6
            descripcion_full = concepto.get('Descripcion', '')
            if len(descripcion_full) > MAX_LENGTH_DESCRIPCION:
                descripcion_short = descripcion_full[:MAX_LENGTH_DESCRIPCION] 
                last_space = descripcion_short.rfind(' ')
                descripcion_short = descripcion_full[:last_space] 
                row_size = 4
            else:
                descripcion_short = descripcion_full
            pdf.cell(24, 6, concepto.get('ClaveProdServ', ''), align='C', border='L')
            pdf.cell(14, 6, str(concepto.get('Cantidad', '')) + '.00', align='C', border=0)
            pdf.cell(20, 6, concepto.get('ClaveUnidad', ''), align='C', border=0)
            pdf.cell(12, 6, concepto.get('Unidad', ''), align='C', border=0)
            pdf.cell(60, row_size, descripcion_short, align='L', border=0)
            pdf.cell(20, 6, '$' + f"{safe_float(concepto.get('ValorUnitario', 0.0)):,.2f}", align='C', border=0)
            importe_impuesto = safe_float(concepto['impuestos'].get('Importe', 0.0) or 0.0)
            pdf.cell(20, 6, '$' + f"{importe_impuesto:,.2f}", align='C', border=0)
            impuesto_total += importe_impuesto
            pdf.cell(20, 6, '$' + f"{safe_float(concepto.get('Importe', 0.0)):,.2f}", align='C', border='R', ln=True)
        if len(descripcion_full) > MAX_LENGTH_DESCRIPCION:
            y = pdf.get_y()
            pdf.set_y(y - 3)
            pdf.cell(70, 4, '', border='L')
            pdf.cell(60, 4, descripcion_full[last_space + 1:], border=0)
            pdf.ln()

        pdf.set_font("Arial", 'B', 7)
        pdf.cell(25, 10, 'OBSERVACIONES:', border='LT')
        pdf.set_font("Arial", '', 7)
        pdf.cell(125, 10, 'Esta factura ampara el documento ' + self.noTicket, border='T')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 6, 'Subtotal:', border='LRTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 6, '$' + f"{safe_float(self.data['subtotal']):,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(150, 6, '', border='LR')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 6, 'IVA 16%:', border='LRTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 6, '$' + f"{impuesto_total:,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(28, 6, 'IMPORTE CON LETRA:', border='LBT')
        pdf.set_font("Arial", '', 7)
        pdf.cell(122, 6, num2words(safe_float(self.data['total']), lang='es', to='currency', currency='MXN'), border='RBT')
        pdf.set_font("Arial", 'B', 7)
        pdf.cell(20, 6, 'Total:', border='RTB', align='C')
        pdf.set_font("Arial", '', 7)
        pdf.cell(20, 6, '$' + f"{safe_float(self.data['total']):,.2f}", border='RTB', align='C', ln=True)
        pdf.cell(190, 3, '', border='LR', ln=True)
        
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
