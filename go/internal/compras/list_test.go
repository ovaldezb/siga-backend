package compras

import (
	"context"
	"encoding/json"
	"testing"
	"time"

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

func listar(t *testing.T, qp map[string]string) (int, pagina) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{"custom:tenant_id": tenant}}}
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

func foliosDe(p pagina) []string {
	out := make([]string, len(p.Items))
	for i, it := range p.Items {
		out[i], _ = it["folio"].(string)
	}
	return out
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	col := c.Database(dbName).Collection("compras")
	base := time.Date(2026, 9, 1, 10, 0, 0, 0, time.UTC)
	compra := func(folio, prov, suc, estado, ref, provNombre string, hora int) bson.D {
		return bson.D{{Key: "folio", Value: folio}, {Key: "proveedor_id", Value: prov}, {Key: "sucursal_id", Value: suc},
			{Key: "estado", Value: estado}, {Key: "referencia_proveedor", Value: ref},
			{Key: "proveedor_snapshot", Value: bson.D{{Key: "nombre", Value: provNombre}}},
			{Key: "createdAt", Value: base.Add(time.Duration(hora) * time.Hour)}}
	}
	if _, err := col.InsertMany(context.Background(), []any{
		compra("COM-1", "p1", "s1", "RECIBIDA", "FAC(9)", "Aceites del Norte", 1),
		compra("COM-2", "p2", "s1", "CANCELADA", "X-2", "Refacciones Zeta", 2),
		compra("COM-3", "p1", "s2", "RECIBIDA", "X-3", "Aceites del Norte", 3),
	}); err != nil {
		t.Fatal(err)
	}

	s, p := listar(t, nil)
	if s != 200 || p.Total != 3 || p.Page != 1 || p.Limit != 50 || foliosDe(p)[0] != "COM-3" || p.Items[2]["createdAt"] != "2026-09-01T11:00:00Z" {
		t.Fatalf("todas: %d %+v", s, p)
	}
	casos := []struct {
		qp   map[string]string
		want []string
	}{
		{map[string]string{"proveedor_id": "p1"}, []string{"COM-3", "COM-1"}},
		{map[string]string{"sucursal_id": "s1", "estado": "cancelada"}, []string{"COM-2"}},
		{map[string]string{"search": "zeta"}, []string{"COM-2"}},
		{map[string]string{"search": "com-1"}, []string{"COM-1"}},
		{map[string]string{"search": "fac(9"}, []string{"COM-1"}}, // Python daba 500 con el "(" suelto
		{map[string]string{"page": "2", "limit": "2"}, []string{"COM-1"}},
	}
	for _, c := range casos {
		_, p := listar(t, c.qp)
		got := foliosDe(p)
		if len(got) != len(c.want) {
			t.Fatalf("%v: %v", c.qp, got)
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Fatalf("%v: %v", c.qp, got)
			}
		}
	}
	if _, p := listar(t, map[string]string{"limit": "999"}); p.Limit != 200 {
		t.Fatalf("tope de 200: %d", p.Limit)
	}
	if s, _ := listar(t, map[string]string{"page": "abc"}); s != 400 {
		t.Fatalf("page inválido: %d", s)
	}
}
