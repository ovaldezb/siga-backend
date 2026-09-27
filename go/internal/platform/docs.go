package platform

import (
	"encoding/json"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"go.mongodb.org/mongo-driver/v2/bson"
)

// IsoUTC replica iso_utc de Python: isoformat() + "Z". isoformat omite la
// fracción cuando los microsegundos son 0 y si no la escribe con seis dígitos.
func IsoUTC(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 == 0 {
		return t.Format("2006-01-02T15:04:05") + "Z"
	}
	return t.Format("2006-01-02T15:04:05.000000") + "Z"
}

// Doc convierte un documento de Mongo al JSON que devolvía Python: `_id` pasa a
// `id`, los ObjectID a hex y las fechas a IsoUTC (como MongoJSONEncoder). Los
// subdocumentos bson.D se vuelven mapas; sin esto se serializarían como arreglos
// de {Key, Value}.
func Doc(m bson.M) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		if k == "_id" {
			out["id"] = plano(v)
			continue
		}
		out[k] = plano(v)
	}
	return out
}

func plano(v any) any {
	switch x := v.(type) {
	case bson.ObjectID:
		return x.Hex()
	case bson.DateTime:
		return IsoUTC(x.Time())
	case time.Time:
		return IsoUTC(x)
	case bson.M:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = plano(e)
		}
		return out
	case bson.D:
		out := make(map[string]any, len(x))
		for _, e := range x {
			out[e.Key] = plano(e.Value)
		}
		return out
	case bson.A:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = plano(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = plano(e)
		}
		return out
	default:
		return v
	}
}

// ParseBody decodifica el body JSON; vacío equivale a {} como en Python.
func ParseBody(req Request, dst any) error {
	body := strings.TrimSpace(req.Body)
	if body == "" {
		body = "{}"
	}
	if err := json.Unmarshal([]byte(body), dst); err != nil {
		return BadRequest("el body no es JSON válido")
	}
	return nil
}

// ParseObjectID devuelve un ClientError (400) en vez del 500 que daba
// ObjectId(valor) con un id mal formado.
func ParseObjectID(valor, campo string) (bson.ObjectID, error) {
	oid, err := bson.ObjectIDFromHex(valor)
	if err != nil {
		return bson.ObjectID{}, BadRequest("%s inválido", campo)
	}
	return oid, nil
}

// ClaimString lee un claim de texto; ausente o de otro tipo devuelve "".
func ClaimString(claims map[string]any, clave string) string {
	s, _ := claims[clave].(string)
	return s
}

// Groups replica get_groups: el claim llega como lista o como string
// ("[ADMIN ASESOR]" o "ADMIN,ASESOR") según el authorizer.
func Groups(claims map[string]any) []string {
	var crudos []string
	switch g := claims["cognito:groups"].(type) {
	case string:
		crudos = strings.Fields(strings.ReplaceAll(strings.Trim(g, "[]"), ",", " "))
	case []any:
		for _, e := range g {
			if s, ok := e.(string); ok {
				crudos = append(crudos, s)
			}
		}
	case []string:
		crudos = g
	}
	out := make([]string, 0, len(crudos))
	for _, s := range crudos {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// IsAdmin es true para ADMIN o SUPER_ADMIN por nombre exacto (a diferencia del
// is_admin de Python, que hace substring).
func IsAdmin(claims map[string]any) bool {
	for _, g := range Groups(claims) {
		if g == "ADMIN" || g == "SUPER_ADMIN" {
			return true
		}
	}
	return false
}

// EsMecanico replica es_mecanico: el mecánico puro, que trabaja la orden pero no
// ve dinero. Si además es ADMIN, SUPER_ADMIN o ASESOR, manda el rol de más alcance.
func EsMecanico(claims map[string]any) bool {
	mecanico := false
	for _, g := range Groups(claims) {
		switch g {
		case "MECANICO":
			mecanico = true
		case "ADMIN", "SUPER_ADMIN", "ASESOR":
			return false
		}
	}
	return mecanico
}

// Truncar corta a n caracteres (no bytes), como s[:n] en Python.
func Truncar(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n])
}

// Numero lee un número de Mongo como float64 (lo que hacía float() en Python):
// int32, int64, double o decimal. Ausente o de otro tipo cuenta como 0.
func Numero(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int32:
		return float64(x)
	case int64:
		return float64(x)
	case int:
		return float64(x)
	case bson.Decimal128:
		r, _ := strconv.ParseFloat(x.String(), 64)
		return r
	}
	return 0
}

// Paginacion lee ?page= y ?limit= como int() de Python (espacios y signo
// permitidos) y calcula el skip. Un valor no numérico o una página que daría
// skip negativo es un ClientError (400), como el ValueError de pymongo.
func Paginacion(qp map[string]string, limitePorDefecto int64) (page, limit, skip int64, err error) {
	entero := func(clave string, defecto int64) (int64, error) {
		raw, ok := qp[clave]
		if !ok {
			return defecto, nil
		}
		n, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
		if err != nil {
			return 0, BadRequest("%s inválido: %q", clave, raw)
		}
		return n, nil
	}
	if page, err = entero("page", 1); err != nil {
		return
	}
	if limit, err = entero("limit", limitePorDefecto); err != nil {
		return
	}
	if skip = (page - 1) * limit; skip < 0 {
		err = BadRequest("page inválido: %d", page)
	}
	return
}

// Verdadero replica la veracidad de Python (bool(v)) para valores de Mongo:
// null, false, 0, "" y listas o documentos vacíos son falsos.
func Verdadero(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	case bson.A:
		return len(x) > 0
	case bson.D:
		return len(x) > 0
	case bson.M:
		return len(x) > 0
	case float64, int32, int64, int, bson.Decimal128:
		return Numero(x) != 0
	}
	return true
}
