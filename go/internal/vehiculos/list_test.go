package vehiculos

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

type paginaVehiculos struct {
	Items      []map[string]any
	Total      int64
	TotalPages int64 `json:"totalPages"`
}

func listar(t *testing.T, tenantID string, qp map[string]string) (int, paginaVehiculos) {
	t.Helper()
	s, _, data := llamar(t, List, func() platform.Request {
		r := req(tenantID, nil)
		r.QueryStringParameters = qp
		return r
	}())
	var p paginaVehiculos
	if len(data) > 0 && string(data) != "null" {
		if err := json.Unmarshal(data, &p); err != nil {
			t.Fatalf("data: %v %s", err, data)
		}
	}
	return s, p
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	now := time.Now().UTC()
	juan := bson.NewObjectID()
	if _, err := db.Collection("clientes").InsertOne(ctx, bson.D{
		{Key: "_id", Value: juan}, {Key: "nombre", Value: "Juan"}, {Key: "apellido_paterno", Value: "Pérez"}, {Key: "telefono", Value: "5511"},
	}); err != nil {
		t.Fatal(err)
	}
	vencidoKm, prontoKm, vencidoDias, sinDatos := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		// km para el aceite <= 0 → vencido.
		bson.D{{Key: "_id", Value: vencidoKm}, {Key: "placas", Value: "AAA-(1)"}, {Key: "marca", Value: "Nissan"}, {Key: "cliente_id", Value: juan.Hex()},
			{Key: "kilometraje", Value: 100500}, {Key: "proximo_cambio_aceite", Value: 100000}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: now.Add(-1 * time.Hour)}, {Key: "año", Value: 2012}},
		// Sin próximo cambio en el vehículo: lo toma de la última OS (a 300 km) → pronto.
		bson.D{{Key: "_id", Value: prontoKm}, {Key: "placas", Value: "BBB"}, {Key: "marca", Value: "Ford"}, {Key: "cliente_id", Value: bson.NewObjectID().Hex()},
			{Key: "kilometraje", Value: 49700}, {Key: "createdAt", Value: now.Add(-2 * time.Hour)}},
		// Última visita hace 400 días → vencido por tiempo.
		bson.D{{Key: "_id", Value: vencidoDias}, {Key: "placas", Value: "CCC"}, {Key: "marca", Value: "VW"}, {Key: "createdAt", Value: now.Add(-3 * time.Hour)},
			{Key: "cliente_id", Value: juan.Hex()}},
		bson.D{{Key: "_id", Value: sinDatos}, {Key: "placas", Value: "DDD"}, {Key: "marca", Value: "VW"}, {Key: "createdAt", Value: now.Add(-4 * time.Hour)},
			{Key: "cliente_id", Value: juan.Hex()}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "vehiculo_id", Value: prontoKm.Hex()}, {Key: "createdAt", Value: now.Add(-24 * time.Hour)}, {Key: "proximo_cambio_aceite", Value: 50000}},
		bson.D{{Key: "vehiculo_id", Value: vencidoDias.Hex()}, {Key: "createdAt", Value: now.Add(-400 * 24 * time.Hour)}},
	}); err != nil {
		t.Fatal(err)
	}

	if s, _ := listar(t, "", nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	s, p := listar(t, tenant, nil)
	if s != 200 || p.Total != 4 || p.TotalPages != 1 || len(p.Items) != 4 {
		t.Fatalf("todos: %d %+v", s, p)
	}
	por := map[string]map[string]any{}
	for _, v := range p.Items {
		por[v["placas"].(string)] = v
	}
	a := por["AAA-(1)"]
	if a["mantenimiento_status"] != "vencido" || a["km_para_aceite"] != -500.0 || a["cliente_nombre"] != "Juan Pérez " ||
		a["cliente_telefono"] != "5511" || a["anio"] != 2012.0 || a["año"] != nil || a["sucursalId"] != "s1" ||
		a["cliente_info"] != nil || a["ultima_os"] != nil || a["ultima_visita_at"] != nil {
		t.Fatalf("AAA: %v", a)
	}
	if b := por["BBB"]; b["mantenimiento_status"] != "pronto" || b["proximo_cambio_aceite"] != 50000.0 ||
		b["dias_desde_ultima_visita"] != 1.0 || b["cliente_nombre"] != "Cliente Desconocido" {
		t.Fatalf("BBB: %v", b)
	}
	if c := por["CCC"]; c["mantenimiento_status"] != "vencido" || c["dias_desde_ultima_visita"] != 400.0 || c["km_para_aceite"] != nil {
		t.Fatalf("CCC: %v", c)
	}
	if d := por["DDD"]; d["mantenimiento_status"] != nil || d["dias_desde_ultima_visita"] != nil {
		t.Fatalf("DDD: %v", d)
	}

	if _, p = listar(t, tenant, map[string]string{"mantenimiento": "VENCIDO"}); p.Total != 2 {
		t.Fatalf("vencidos: %+v", p)
	}
	if _, p = listar(t, tenant, map[string]string{"mantenimiento": "pronto"}); p.Total != 1 || p.Items[0]["placas"] != "BBB" {
		t.Fatalf("pronto: %+v", p)
	}
	// Umbral de km más chico: BBB (300 km) deja de estar "pronto".
	if _, p = listar(t, tenant, map[string]string{"mantenimiento": "pronto", "km_umbral": "100"}); p.Total != 0 || p.Items == nil {
		t.Fatalf("umbral 100: %+v", p)
	}
	if _, p = listar(t, tenant, map[string]string{"search": "aaa-(1"}); p.Total != 1 {
		t.Fatalf("búsqueda literal: %+v", p)
	}
	if _, p = listar(t, tenant, map[string]string{"cliente_id": juan.Hex(), "limit": "2", "page": "2"}); p.Total != 3 || len(p.Items) != 1 || p.TotalPages != 2 {
		t.Fatalf("por cliente paginado: %+v", p)
	}

	// Datos legacy que tronaban toda la consulta con 500 (el filtro de
	// mantenimiento no mostraba nada): ahora se convierten o cuentan como sin dato.
	legacy := bson.NewObjectID()
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "placas", Value: "LEG-VACIO"}, {Key: "cliente_id", Value: ""}, {Key: "createdAt", Value: now.Add(-5 * time.Hour)}},
		bson.D{{Key: "placas", Value: "LEG-KMTEXTO"}, {Key: "kilometraje", Value: "99900"}, {Key: "proximo_cambio_aceite", Value: 100000},
			{Key: "proximo_cambio_aceite_fecha", Value: 202612}, {Key: "createdAt", Value: now.Add(-6 * time.Hour)}},
		bson.D{{Key: "_id", Value: legacy}, {Key: "placas", Value: "LEG-OSTEXTO"}, {Key: "createdAt", Value: now.Add(-7 * time.Hour)}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertOne(ctx, bson.D{
		{Key: "vehiculo_id", Value: legacy.Hex()}, {Key: "createdAt", Value: now.Add(-400 * 24 * time.Hour).Format(time.RFC3339)},
	}); err != nil {
		t.Fatal(err)
	}
	s, p = listar(t, tenant, map[string]string{"mantenimiento": "vencido", "limit": "100"})
	placas := map[string]bool{}
	for _, v := range p.Items {
		placas[v["placas"].(string)] = true
	}
	if s != 200 || p.Total != 3 || !placas["LEG-OSTEXTO"] {
		t.Fatalf("vencidos con legacy: %d %v", s, placas)
	}
	s, p = listar(t, tenant, map[string]string{"mantenimiento": "pronto", "limit": "100"})
	if s != 200 || p.Total != 2 {
		t.Fatalf("pronto con legacy: %d %+v", s, p)
	}
	if s, p = listar(t, tenant, map[string]string{"limit": "100"}); s != 200 || p.Total != 7 {
		t.Fatalf("todos con legacy: %d %+v", s, p)
	}

	// El kilometraje se captura en la OS, casi nunca en el vehículo: se toma de
	// la última OS. Antes km_para_aceite salía vacío y el filtro no mostraba nada.
	soloOS := bson.NewObjectID()
	if _, err := db.Collection("vehiculos").InsertOne(ctx, bson.D{
		{Key: "_id", Value: soloOS}, {Key: "placas", Value: "KM-EN-OS"}, {Key: "createdAt", Value: now.Add(-8 * time.Hour)},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "vehiculo_id", Value: soloOS.Hex()}, {Key: "createdAt", Value: now.Add(-48 * time.Hour)},
			{Key: "kilometraje", Value: 70000}, {Key: "proximo_cambio_aceite", Value: 75000}},
		bson.D{{Key: "vehiculo_id", Value: soloOS.Hex()}, {Key: "createdAt", Value: now.Add(-24 * time.Hour)},
			{Key: "kilometraje", Value: 74800}, {Key: "proximo_cambio_aceite", Value: 75000}},
	}); err != nil {
		t.Fatal(err)
	}
	_, p = listar(t, tenant, map[string]string{"mantenimiento": "pronto", "limit": "100"})
	var kmOS map[string]any
	for _, v := range p.Items {
		if v["placas"] == "KM-EN-OS" {
			kmOS = v
		}
	}
	if kmOS == nil || kmOS["kilometraje"] != 74800.0 || kmOS["km_para_aceite"] != 200.0 {
		t.Fatalf("kilometraje de la última OS: %v", kmOS)
	}
}
