package clientes

import (
	"context"
	"strings"
	"unicode"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"
)

// La cartera es del taller: un cliente dado de alta en una sucursal se atiende
// en cualquier otra. Para que no se vuelva a capturar, el alta y la limpieza de
// duplicados comparan los datos que identifican a una persona normalizados
// (sin espacios, guiones, mayúsculas ni acentos), igual que los capturan
// distintos asesores en distintas sucursales.

// RFC genéricos del SAT: los comparten todos los clientes sin datos fiscales.
var rfcGenericos = map[string]bool{"XAXX010101000": true, "XEXX010101000": true}

// telefonoClave deja los últimos 10 dígitos (quita lada +52, espacios y
// guiones). Descarta teléfonos de relleno como 0000000000 o 5555555555.
func telefonoClave(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	d := b.String()
	if len(d) < 10 {
		return ""
	}
	d = d[len(d)-10:]
	if strings.Count(d, d[:1]) == len(d) {
		return ""
	}
	return d
}

func emailClave(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	if !strings.Contains(s, "@") {
		return ""
	}
	return s
}

func rfcClave(s string) string {
	s = strings.ToUpper(strings.Join(strings.Fields(s), ""))
	if len(s) < 12 || rfcGenericos[s] {
		return ""
	}
	return s
}

// placasClave quita todo lo que no sea letra o dígito: "ABC-12-34" = "abc 1234".
func placasClave(s string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(s) {
		if (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		}
	}
	if b.Len() < 4 {
		return ""
	}
	return b.String()
}

var sinAcento = strings.NewReplacer(
	"á", "a", "é", "e", "í", "i", "ó", "o", "ú", "u", "ü", "u", "ñ", "n",
	"à", "a", "è", "e", "ì", "i", "ò", "o", "ù", "u",
)

func textoClave(s string) string {
	s = sinAcento.Replace(strings.ToLower(s))
	campos := strings.FieldsFunc(s, func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) })
	return strings.Join(campos, " ")
}

// Nombres de mostrador: no identifican a nadie.
var nombresGenericos = map[string]bool{
	"publico general": true, "publico en general": true, "mostrador": true, "cliente": true,
	"cliente general": true, "varios": true,
}

// nombreClave exige nombre y apellido paterno: "Juan" a secas no sirve para
// decir que dos registros son la misma persona.
func nombreClave(c map[string]any) string {
	nombre := textoClave(texto(c["nombre"]))
	paterno := textoClave(texto(c["apellido_paterno"]))
	if nombre == "" || paterno == "" {
		return ""
	}
	clave := strings.TrimSpace(nombre + " " + paterno + " " + textoClave(texto(c["apellido_materno"])))
	if nombresGenericos[clave] || nombresGenericos[nombre+" "+paterno] {
		return ""
	}
	return clave
}

func texto(v any) string {
	s, _ := v.(string)
	return s
}

// Motivos de coincidencia, de más a menos confiable. El front los muestra tal
// cual para que el asesor decida si es la misma persona.
const (
	PorTelefono = "telefono"
	PorEmail    = "email"
	PorRFC      = "rfc"
	PorPlacas   = "placas"
	PorNombre   = "nombre"
)

// nombresSucursales mapea id → nombre de las sucursales del taller. Los no-admin
// sólo traen en su sesión las sucursales asignadas, así que el nombre de la de
// origen de un cliente lo resuelve el backend.
func nombresSucursales(ctx context.Context, db *mongo.Database) (map[string]string, error) {
	cur, err := db.Collection("sucursales").Find(ctx, bson.D{},
		options.Find().SetProjection(bson.D{{Key: "nombre", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make(map[string]string, len(docs))
	for _, d := range docs {
		if oid, ok := d["_id"].(bson.ObjectID); ok {
			out[oid.Hex()] = texto(d["nombre"])
		}
	}
	return out, nil
}

// sucursalDe lee la sucursal de un documento ya aplanado con platform.Doc. Los
// clientes la exponen como `sucursalId`; OS, ventas y citas como `sucursal_id`.
func sucursalDe(d map[string]any) string {
	if s := texto(d["sucursalId"]); s != "" {
		return s
	}
	return texto(d["sucursal_id"])
}

// conNombreSucursal agrega `sucursalNombre` a cada documento que tenga sucursal.
func conNombreSucursal(docs []map[string]any, nombres map[string]string) {
	for _, d := range docs {
		if n := nombres[sucursalDe(d)]; n != "" {
			d["sucursalNombre"] = n
		}
	}
}
