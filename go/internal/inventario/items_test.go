package inventario

import (
	"context"
	"encoding/json"
	"slices"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/testmongo"
)

type paginaItems struct {
	Items      []map[string]any
	Total      int64
	Page       int64
	Limit      int64
	Sucursales []map[string]any
}

func listarItems(t *testing.T, cl map[string]any, qp map[string]string) (int, string, paginaItems) {
	t.Helper()
	s, m, data := llamar(t, Items, req(cl, "", qp))
	var p paginaItems
	if len(data) > 0 && string(data) != "null" {
		if err := json.Unmarshal(data, &p); err != nil {
			t.Fatalf("data: %v (%s)", err, data)
		}
	}
	return s, m, p
}

func nombresItems(p paginaItems) []string {
	out := make([]string, len(p.Items))
	for i, it := range p.Items {
		out[i], _ = it["nombre"].(string)
	}
	slices.Sort(out)
	return out
}

func TestItemsSinBaseDeDatos(t *testing.T) {
	if s, _, _ := listarItems(t, map[string]any{}, nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if _, err := Items(context.Background(), req(claims("a@t.mx", "ADMIN"), "", map[string]string{"limit": "x"})); err == nil {
		t.Fatal("limit inválido debía dar ClientError (400)")
	}
}

func TestItemsContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	matriz, norte, cerrada := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("sucursales").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: norte}, {Key: "nombre", Value: "Norte"}},
		bson.D{{Key: "_id", Value: matriz}, {Key: "nombre", Value: "Matriz"}, {Key: "activa", Value: true}},
		bson.D{{Key: "_id", Value: cerrada}, {Key: "nombre", Value: "Cerrada"}, {Key: "activa", Value: false}},
	}); err != nil {
		t.Fatal(err)
	}
	m, n, x := matriz.Hex(), norte.Hex(), cerrada.Hex()
	item := func(nombre, suc, tipo, noParte string, stock any, extra ...bson.E) bson.D {
		d := bson.D{{Key: "nombre", Value: nombre}, {Key: "sucursal_id", Value: suc}, {Key: "tipo", Value: tipo},
			{Key: "no_parte", Value: noParte}, {Key: "stock", Value: stock}}
		return append(d, extra...)
	}
	if _, err := db.Collection("items").InsertMany(ctx, []any{
		item("Filtro aceite", m, "PRODUCTO", "FA-100", 5),
		item("Filtro aceite", n, "PRODUCTO", "fa-100", 2),
		item("Filtro aceite", x, "PRODUCTO", "FA-100", 1), // mayúsculas/minúsculas mixtas no se buscan, igual que en Python
		item("Balata (del)", m, "PRODUCTO", "BAL+1", 0, bson.E{Key: "marca", Value: "Brembo"}),
		item("Mano de obra", m, "SERVICIO", "", nil),
		item("Captura OS", m, "PRODUCTO", "CAP-1", 0, bson.E{Key: "maneja_inventario", Value: false}),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "cajero@t.mx"}, {Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: m}}}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := claims("admin@t.mx", "ADMIN")
	cajero := claims("cajero@t.mx", "CAJERO")

	s, msg, p := listarItems(t, admin, nil)
	if s != 200 || msg != "Items obtenidos" || p.Total != 6 || p.Page != 1 || p.Limit != 50 || p.Sucursales != nil {
		t.Fatalf("admin todo: %d %q %+v", s, msg, p)
	}
	if p.Items[0]["sucursalId"] == nil || p.Items[0]["sucursal_id"] != nil || p.Items[0]["existencias"] != nil {
		t.Fatalf("serialización: %v", p.Items[0])
	}

	casos := []struct {
		nombre string
		cl     map[string]any
		qp     map[string]string
		want   []string
	}{
		{"cajero: solo su sucursal", cajero, nil, []string{"Balata (del)", "Captura OS", "Filtro aceite", "Mano de obra"}},
		{"soloInventario quita capturas, deja servicios", cajero, map[string]string{"soloInventario": "TRUE"}, []string{"Balata (del)", "Filtro aceite", "Mano de obra"}},
		{"tipo", cajero, map[string]string{"tipo": "SERVICIO"}, []string{"Mano de obra"}},
		{"search por palabras (AND)", admin, map[string]string{"search": "filtro  ACEITE", "sucursalId": n}, []string{"Filtro aceite"}},
		{"search con símbolos escapados", admin, map[string]string{"search": "(del) bal+1"}, []string{"Balata (del)"}},
		{"search en marca", admin, map[string]string{"search": "brembo"}, []string{"Balata (del)"}},
		{"search sin coincidencia de ambas", admin, map[string]string{"search": "filtro brembo"}, []string{}},
	}
	for _, c := range casos {
		s, _, p := listarItems(t, c.cl, c.qp)
		if s != 200 || !slices.Equal(nombresItems(p), c.want) {
			t.Fatalf("%s: %d %v", c.nombre, s, nombresItems(p))
		}
	}
	if s, _, _ := listarItems(t, cajero, map[string]string{"sucursal_id": n}); s != 403 {
		t.Fatalf("cajero en sucursal ajena: %d", s)
	}
	if _, _, p := listarItems(t, admin, map[string]string{"page": "2", "limit": "4"}); p.Total != 6 || len(p.Items) != 2 {
		t.Fatalf("paginación: %+v", p)
	}

	// Existencias cruzadas del filtro en Matriz: Norte (activa por omisión),
	// Matriz y Cerrada (inactiva, pero con stock).
	_, _, p = listarItems(t, cajero, map[string]string{"existencias": "true", "search": "filtro"})
	if len(p.Items) != 1 || len(p.Sucursales) != 3 || p.Sucursales[0]["nombre"] != "Cerrada" || p.Sucursales[2]["activa"] != true {
		t.Fatalf("existencias: %+v", p)
	}
	ex := p.Items[0]["existencias"].([]any)
	if len(ex) != 3 || p.Items[0]["stock_total"] != 8.0 {
		t.Fatalf("existencias del filtro: %v total=%v", ex, p.Items[0]["stock_total"])
	}
	for _, e := range ex {
		e := e.(map[string]any)
		want := map[string]float64{m: 5, n: 2, x: 1}[e["sucursal_id"].(string)]
		if e["stock"] != want || e["es_actual"] != (e["sucursal_id"] == m) {
			t.Fatalf("existencia: %v", e)
		}
	}

	// La balata no tiene stock en otras sucursales: la inactiva sin stock se omite.
	_, _, p = listarItems(t, cajero, map[string]string{"existencias": "true", "search": "balata"})
	ex = p.Items[0]["existencias"].([]any)
	if len(ex) != 2 || p.Items[0]["stock_total"] != 0.0 {
		t.Fatalf("balata: %v", ex)
	}
	// Servicios y capturas no llevan existencias.
	_, _, p = listarItems(t, cajero, map[string]string{"existencias": "true", "search": "mano"})
	if _, ok := p.Items[0]["existencias"]; ok {
		t.Fatalf("servicio con existencias: %v", p.Items[0])
	}
}
