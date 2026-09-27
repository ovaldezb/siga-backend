// Package talleres atiende la configuración del taller del usuario logueado
// (port de get_my_modulos_handler en src/handlers/admin/talleres_manager.py).
// El resto de talleres (alta, logo, Openpay) sigue en Python.
package talleres

import (
	"context"
	"regexp"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	estadoActivo   = "ACTIVO"
	estadoInactivo = "INACTIVO"
)

var proyeccion = bson.D{
	{Key: "_id", Value: 0},
	{Key: "modulos", Value: 1}, {Key: "estado", Value: 1}, {Key: "logoUrl", Value: 1},
	{Key: "nombreComercial", Value: 1}, {Key: "direccion", Value: 1}, {Key: "adminTelefono", Value: 1},
	{Key: "proximaFechaCorte", Value: 1}, {Key: "proximaFechaPago", Value: 1},
	{Key: "precioSuscripcion", Value: 1}, {Key: "mesesCargo", Value: 1}, {Key: "diasPrueba", Value: 1},
	{Key: "fechaSuscripcion", Value: 1}, {Key: "usuarios", Value: 1}, {Key: "sucursales", Value: 1},
	{Key: "openpayClabe", Value: 1}, {Key: "openpayBank", Value: 1},
	{Key: "openpayAgreement", Value: 1}, {Key: "openpayReference", Value: 1},
}

// ahora es reemplazable en pruebas.
var ahora = func() time.Time { return time.Now().UTC() }

// Formatos que acepta datetime.fromisoformat de Python para las fechas que
// llegan como texto. El offset se descarta después, como hacía
// .replace(tzinfo=None): cuenta la hora de reloj escrita, no el instante.
var (
	reOffset   = regexp.MustCompile(`([+-]\d{2}(:?\d{2})?(:?\d{2}(\.\d+)?)?|Z)$`)
	formatosIn = []string{
		"2006-01-02T15:04:05.999999999",
		"2006-01-02T15:04:05",
		"2006-01-02T15:04",
		"2006-01-02T15",
		"2006-01-02 15:04:05.999999999",
		"2006-01-02 15:04:05",
		"2006-01-02 15:04",
		"2006-01-02",
		"20060102T150405",
		"20060102",
	}
)

// parseFechaPython devuelve la hora de reloj (como UTC) de un texto ISO, o false
// si no se reconoce; en ese caso Python ignoraba el ValueError y no desactivaba.
func parseFechaPython(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if len(s) > 10 {
		s = reOffset.ReplaceAllString(s, "")
	}
	for _, f := range formatosIn {
		if t, err := time.Parse(f, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// fechaPago normaliza proximaFechaPago (texto o fecha de Mongo) a UTC.
func fechaPago(v any) (time.Time, bool) {
	switch x := v.(type) {
	case string:
		return parseFechaPython(x)
	case bson.DateTime:
		return x.Time().UTC(), true
	case time.Time:
		return x.UTC(), true
	}
	return time.Time{}, false
}

// vencido replica la regla de Python: solo un taller ACTIVO con fecha de pago
// ya alcanzada (en UTC) pasa a INACTIVO.
func vencido(taller bson.M, now time.Time) bool {
	if estado(taller) != estadoActivo {
		return false
	}
	pago, ok := fechaPago(taller["proximaFechaPago"])
	return ok && !now.Before(pago)
}

func estado(taller bson.M) any {
	if v, ok := taller["estado"]; ok {
		return v
	}
	return estadoActivo
}

// get replica dict.get(clave, defecto): el defecto solo aplica si la clave no
// existe; un null guardado se devuelve como null.
func get(m map[string]any, clave string, defecto any) any {
	if v, ok := m[clave]; ok {
		return v
	}
	return defecto
}

// Modulos atiende GET /talleres/me/modulos: módulos, estado de suscripción y
// datos del taller. Lo llama el login en paralelo con /usuarios/me.
func Modulos(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		// Sin tenant es el SUPER_ADMIN global de la plataforma.
		return platform.JSON(req, 200, "Módulos para admin global", map[string]any{"modulos": []string{"*"}}), nil
	}

	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}
	col := db.Collection("talleres")

	var taller bson.M
	err = col.FindOne(ctx, bson.D{{Key: "tenantId", Value: tenantID}},
		options.FindOne().SetProjection(proyeccion)).Decode(&taller)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Taller no encontrado", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}

	if now := ahora(); vencido(taller, now) {
		_, err := col.UpdateOne(ctx, bson.D{{Key: "tenantId", Value: tenantID}},
			bson.D{{Key: "$set", Value: bson.D{
				{Key: "estado", Value: estadoInactivo},
				{Key: "updatedAt", Value: now},
			}}})
		if err != nil {
			return platform.Response{}, err
		}
		taller["estado"] = estadoInactivo
	}

	t := platform.Doc(taller) // fechas de Mongo → ISO; los textos pasan igual
	return platform.JSON(req, 200, "Configuración recuperada", map[string]any{
		"modulos":           get(t, "modulos", []any{}),
		"estado":            get(t, "estado", estadoActivo),
		"logoUrl":           t["logoUrl"],
		"nombreTaller":      get(t, "nombreComercial", "SIGA"),
		"direccion":         get(t, "direccion", "Dirección no especificada"),
		"adminTelefono":     get(t, "adminTelefono", "Teléfono no especificado"),
		"proximaFechaCorte": t["proximaFechaCorte"],
		"proximaFechaPago":  t["proximaFechaPago"],
		"precioSuscripcion": t["precioSuscripcion"],
		"mesesCargo":        t["mesesCargo"],
		"diasPrueba":        t["diasPrueba"],
		"fechaSuscripcion":  t["fechaSuscripcion"],
		"usuarios":          t["usuarios"],
		"sucursales":        t["sucursales"],
		"openpayClabe":      get(t, "openpayClabe", ""),
		"openpayBank":       get(t, "openpayBank", "BBVA Bancomer"),
		"openpayAgreement":  get(t, "openpayAgreement", "1422286"),
		"openpayReference":  get(t, "openpayReference", ""),
	}), nil
}
