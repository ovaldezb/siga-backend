package proveedores

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

type pagina struct {
	Items []map[string]any
	Total int64
	Page  int64
	Limit int64
}

func llamar(t *testing.T, tenantID string, qp map[string]string) (int, pagina) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	claims := map[string]any{}
	if tenantID != "" {
		claims["custom:tenant_id"] = tenantID
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := List(context.Background(), r)
	if err != nil {
		if _, ok := err.(*platform.ClientError); ok {
			return 400, pagina{}
		}
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data pagina }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func nombres(p pagina) []string {
	out := make([]string, len(p.Items))
	for i, it := range p.Items {
		out[i], _ = it["nombre"].(string)
	}
	return out
}

func TestListContraMongo(t *testing.T) {
	const tenant, dbName = "aaaa-5555", "t_aaaa5555"
	c := testmongo.Conectar(t, dbName)
	if _, err := c.Database(dbName).Collection("proveedores").InsertMany(context.Background(), []any{
		bson.D{{Key: "nombre", Value: "Refacciones Zeta"}, {Key: "categoria", Value: "Frenos"}},
		bson.D{{Key: "nombre", Value: "Aceites del Norte"}, {Key: "rfc", Value: "ADN010101AB1"}},
		bson.D{{Key: "nombre", Value: "Bujías (MX)"}, {Key: "contacto", Value: "Laura"}},
	}); err != nil {
		t.Fatal(err)
	}

	if s, _ := llamar(t, "", nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	s, p := llamar(t, tenant, nil)
	if s != 200 || p.Total != 3 || p.Page != 1 || p.Limit != 20 || nombres(p)[0] != "Aceites del Norte" || p.Items[0]["id"] == nil {
		t.Fatalf("todos: %d %+v", s, p)
	}
	if _, p := llamar(t, tenant, map[string]string{"page": "2", "limit": "2"}); p.Total != 3 || len(p.Items) != 1 || nombres(p)[0] != "Refacciones Zeta" {
		t.Fatalf("página 2: %+v", p)
	}
	for q, want := range map[string]string{"frenos": "Refacciones Zeta", "adn0101": "Aceites del Norte", "laura": "Bujías (MX)", "(mx)": "Bujías (MX)"} {
		if _, p := llamar(t, tenant, map[string]string{"q": " " + q + " "}); p.Total != 1 || nombres(p)[0] != want {
			t.Fatalf("q=%q: %+v", q, p)
		}
	}
	if _, p := llamar(t, tenant, map[string]string{"q": "nada"}); p.Items == nil || len(p.Items) != 0 {
		t.Fatalf("sin resultados debe ser lista: %+v", p)
	}
	if s, _ := llamar(t, tenant, map[string]string{"page": "0"}); s != 400 {
		t.Fatalf("page 0: %d", s)
	}
}
