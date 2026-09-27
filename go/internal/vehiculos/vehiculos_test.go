package vehiculos

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-8888"
	dbName = "t_aaaa8888"
)

func req(tenantID string, path map[string]string) platform.Request {
	r := platform.Request{PathParameters: path}
	claims := map[string]any{}
	if tenantID != "" {
		claims["custom:tenant_id"] = tenantID
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func llamar(t *testing.T, h platform.Handler, r platform.Request) (int, string, json.RawMessage) {
	t.Helper()
	resp, err := h(context.Background(), r)
	if err != nil {
		if _, ok := err.(*platform.ClientError); ok {
			return 400, err.Error(), nil
		}
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct {
		Message string
		Data    json.RawMessage
	}
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Message, s.Data
}

func TestGetContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	cli := bson.NewObjectID()
	if _, err := db.Collection("clientes").InsertOne(ctx, bson.D{
		{Key: "_id", Value: cli}, {Key: "nombre", Value: "Juan"}, {Key: "apellido_paterno", Value: "Pérez"},
	}); err != nil {
		t.Fatal(err)
	}
	conDueno, huerfano, legacy, ambos := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: conDueno}, {Key: "cliente_id", Value: cli.Hex()}, {Key: "placas", Value: "ABC-123"},
			{Key: "sucursal_id", Value: "s1"}, {Key: "anio", Value: 2020},
			{Key: "createdAt", Value: time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)}},
		bson.D{{Key: "_id", Value: huerfano}, {Key: "cliente_id", Value: "no-es-oid"}},
		bson.D{{Key: "_id", Value: legacy}, {Key: "año", Value: 2015}},
		bson.D{{Key: "_id", Value: ambos}, {Key: "año", Value: 2010}, {Key: "anio", Value: 2011}},
	}); err != nil {
		t.Fatal(err)
	}

	if s, _, _ := llamar(t, Get, req("", map[string]string{"id": conDueno.Hex()})); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _, _ := llamar(t, Get, req(tenant, map[string]string{"id": "nope"})); s != 400 {
		t.Fatalf("id inválido: %d", s)
	}
	if s, _, _ := llamar(t, Get, req(tenant, map[string]string{"id": bson.NewObjectID().Hex()})); s != 404 {
		t.Fatalf("inexistente: %d", s)
	}

	get := func(oid bson.ObjectID) map[string]any {
		t.Helper()
		s, m, data := llamar(t, Get, req(tenant, map[string]string{"id": oid.Hex()}))
		var v map[string]any
		if err := json.Unmarshal(data, &v); err != nil || s != 200 || m != "Vehículo obtenido" {
			t.Fatalf("%s: %d %q %s", oid.Hex(), s, m, data)
		}
		return v
	}
	v := get(conDueno)
	if v["id"] != conDueno.Hex() || v["cliente_nombre"] != "Juan Pérez " || v["sucursalId"] != "s1" ||
		v["sucursal_id"] != nil || v["anio"] != 2020.0 || v["createdAt"] != "2026-01-02T03:04:05Z" ||
		v["cliente_info"] != nil || v["cliente_oid"] != nil {
		t.Fatalf("con dueño: %v", v)
	}
	if v := get(huerfano); v["cliente_nombre"] != "Cliente Desconocido" {
		t.Fatalf("huérfano: %v", v)
	}
	if v := get(legacy); v["anio"] != 2015.0 || v["año"] != nil {
		t.Fatalf("legacy: %v", v)
	}
	if v := get(ambos); v["anio"] != 2011.0 || v["año"] != nil {
		t.Fatalf("año y anio: %v", v)
	}
}

func TestDecodeVIN(t *testing.T) {
	var pedido string
	status := http.StatusOK
	cuerpo := `{"Count":1,"Results":[{"Make":"NISSAN","Model":"Versa","ModelYear":"2020"}]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pedido = r.URL.RequestURI()
		if !strings.Contains(r.UserAgent(), "Mozilla") {
			t.Errorf("User-Agent: %q", r.UserAgent())
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(cuerpo))
	}))
	defer srv.Close()
	nhtsaURL = srv.URL + "/api/vehicles/DecodeVinValues/"
	t.Cleanup(func() { nhtsaURL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/" })

	vin := "3N1CN8EV0LL800001"
	if s, _, _ := llamar(t, DecodeVIN, req("", map[string]string{"vin": vin})); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	for _, malo := range []string{"", "CORTO", vin + "X"} {
		if s, m, _ := llamar(t, DecodeVIN, req(tenant, map[string]string{"vin": malo})); s != 400 || m != "El VIN debe tener exactamente 17 caracteres." {
			t.Fatalf("vin %q: %d %q", malo, s, m)
		}
	}

	s, m, data := llamar(t, DecodeVIN, req(tenant, map[string]string{"vin": " " + vin + " "}))
	if s != 200 || m != "VIN decodificado exitosamente" || string(data) != cuerpo {
		t.Fatalf("ok: %d %q %s", s, m, data)
	}
	if pedido != "/api/vehicles/DecodeVinValues/"+vin+"?format=json" {
		t.Fatalf("URL pedida: %s", pedido)
	}

	status = http.StatusInternalServerError
	if s, _, _ := llamar(t, DecodeVIN, req(tenant, map[string]string{"vin": vin})); s != 502 {
		t.Fatalf("NHTSA 500: %d", s)
	}
	status, cuerpo = http.StatusOK, "<html>mantenimiento</html>"
	if s, _, _ := llamar(t, DecodeVIN, req(tenant, map[string]string{"vin": vin})); s != 502 {
		t.Fatalf("NHTSA sin JSON: %d", s)
	}
	srv.Close()
	if s, _, _ := llamar(t, DecodeVIN, req(tenant, map[string]string{"vin": vin})); s != 502 {
		t.Fatalf("NHTSA caída: %d", s)
	}
}
