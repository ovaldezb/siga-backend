package recordatorios

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "bbbb7777"
	dbName = "t_bbbb7777"
)

func TestEvaluar(t *testing.T) {
	casos := []struct {
		nombre string
		v      map[string]any
		estado string
	}{
		{"aceite vencido por km", map[string]any{"kilometraje": 100500, "proximo_cambio_aceite": 100000}, EstadoVencido},
		{"aceite pronto por km", map[string]any{"kilometraje": 99700, "proximo_cambio_aceite": 100000}, EstadoPronto},
		{"lejos por km", map[string]any{"kilometraje": 90000, "proximo_cambio_aceite": 100000}, ""},
		{"fecha vencida", map[string]any{"proximo_cambio_aceite_fecha": "2026-09"}, EstadoVencido},
		{"fecha del mes", map[string]any{"proximo_cambio_aceite_fecha": "2026-10-20"}, EstadoVencido},
		{"fecha el próximo mes", map[string]any{"proximo_cambio_bujias_fecha": "2026-11"}, EstadoPronto},
		{"fecha lejana", map[string]any{"proximo_cambio_aceite_fecha": "2027-03"}, ""},
		{"sin venir un año", map[string]any{"dias_desde_ultima_visita": 400}, EstadoVencido},
		{"sin venir medio año", map[string]any{"dias_desde_ultima_visita": int32(200)}, EstadoPronto},
		{"sin datos", map[string]any{}, ""},
	}
	for _, c := range casos {
		estado, motivos := evaluar(c.v, "2026-10", "2026-11")
		if estado != c.estado {
			t.Errorf("%s: estado %q, esperaba %q (%v)", c.nombre, estado, c.estado, motivos)
		}
		if estado != "" && len(motivos) == 0 {
			t.Errorf("%s: sin motivos", c.nombre)
		}
	}
}

func TestSeguimientoVigente(t *testing.T) {
	ahora := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	futuro := bson.NewDateTimeFromTime(ahora.AddDate(0, 0, 10))
	pasado := bson.NewDateTimeFromTime(ahora.AddDate(0, 0, -1))
	if !seguimientoVigente(bson.M{"estado": "POSPUESTO", "hasta": futuro}, ahora) {
		t.Error("pospuesto a futuro debe ocultarlo")
	}
	if seguimientoVigente(bson.M{"estado": "CONTACTADO", "hasta": pasado}, ahora) {
		t.Error("vencido el plazo debe volver a aparecer")
	}
	if !seguimientoVigente(bson.D{{Key: "estado", Value: "DESCARTADO"}}, ahora) {
		t.Error("descartado sin fecha no vuelve")
	}
	if seguimientoVigente(nil, ahora) {
		t.Error("sin seguimiento no se oculta")
	}
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	now := time.Now().UTC()

	cli := bson.NewObjectID()
	if _, err := db.Collection("clientes").InsertOne(ctx, bson.D{
		{Key: "_id", Value: cli}, {Key: "nombre", Value: "Ana"}, {Key: "apellido_paterno", Value: "López"}, {Key: "telefono", Value: "4491234567"},
	}); err != nil {
		t.Fatal(err)
	}
	vencido, pronto, enTaller, pospuesto, alDia, otraSucursal :=
		bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	veh := func(id bson.ObjectID, suc string, extra ...bson.E) bson.D {
		return append(bson.D{{Key: "_id", Value: id}, {Key: "cliente_id", Value: cli.Hex()}, {Key: "sucursal_id", Value: suc},
			{Key: "placas", Value: id.Hex()[:6]}, {Key: "createdAt", Value: now}}, extra...)
	}
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		veh(vencido, "s1", bson.E{Key: "kilometraje", Value: 100500}, bson.E{Key: "proximo_cambio_aceite", Value: 100000}),
		veh(pronto, "s1", bson.E{Key: "kilometraje", Value: 99800}, bson.E{Key: "proximo_cambio_aceite", Value: 100000}),
		veh(enTaller, "s1", bson.E{Key: "kilometraje", Value: 100500}, bson.E{Key: "proximo_cambio_aceite", Value: 100000}),
		veh(pospuesto, "s1", bson.E{Key: "kilometraje", Value: 100500}, bson.E{Key: "proximo_cambio_aceite", Value: 100000},
			bson.E{Key: "recordatorio_servicio", Value: bson.D{{Key: "estado", Value: "POSPUESTO"}, {Key: "hasta", Value: now.AddDate(0, 0, 15)}}}),
		veh(alDia, "s1", bson.E{Key: "kilometraje", Value: 50000}, bson.E{Key: "proximo_cambio_aceite", Value: 55000}),
		veh(otraSucursal, "s2", bson.E{Key: "kilometraje", Value: 100500}, bson.E{Key: "proximo_cambio_aceite", Value: 100000}),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertOne(ctx, bson.D{
		{Key: "vehiculo_id", Value: enTaller.Hex()}, {Key: "estado", Value: "EN_PROCESO"}, {Key: "createdAt", Value: now},
	}); err != nil {
		t.Fatal(err)
	}

	r := platform.Request{QueryStringParameters: map[string]string{"sucursal_id": "s1"}}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{
		"custom:tenant_id": tenant, "cognito:groups": "ADMIN"}}}
	resp, err := List(ctx, r)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("status %d err %v %s", resp.StatusCode, err, resp.Body)
	}
	var sobre struct {
		Data struct {
			Items    []map[string]any
			Total    int
			Vencidos int
		}
	}
	if err := json.Unmarshal([]byte(resp.Body), &sobre); err != nil {
		t.Fatal(err)
	}
	ids := []string{}
	for _, it := range sobre.Data.Items {
		ids = append(ids, it["id"].(string))
	}
	if len(ids) != 2 || ids[0] != vencido.Hex() || ids[1] != pronto.Hex() {
		t.Fatalf("esperaba [vencido, pronto], llegó %v", ids)
	}
	if sobre.Data.Vencidos != 1 {
		t.Errorf("vencidos = %d", sobre.Data.Vencidos)
	}
	if got := sobre.Data.Items[0]["cliente_nombre"]; got != "Ana López" {
		t.Errorf("cliente_nombre = %q", got)
	}

	// Con todos=1 aparece también el pospuesto.
	r.QueryStringParameters["todos"] = "1"
	resp, _ = List(ctx, r)
	_ = json.Unmarshal([]byte(resp.Body), &sobre)
	if sobre.Data.Total != 3 {
		t.Errorf("todos=1 total = %d, esperaba 3", sobre.Data.Total)
	}
}
