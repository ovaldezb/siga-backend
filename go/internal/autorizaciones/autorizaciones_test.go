package autorizaciones

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const tenant = "11111111-2222-3333-4444-555555555555"

type sobre struct {
	Success bool            `json:"success"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func req(grupos string, body any) platform.Request {
	r := platform.Request{}
	claims := map[string]any{"sub": "sub-cajero", "email": "cajero@taller.com", "custom:tenant_id": tenant}
	if grupos != "" {
		claims["cognito:groups"] = grupos
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	if body != nil {
		b, _ := json.Marshal(body)
		r.Body = string(b)
	}
	return r
}

func llamar(t *testing.T, h platform.Handler, r platform.Request, data any) (int, string) {
	t.Helper()
	resp, err := h(context.Background(), r)
	if err != nil {
		var ce *platform.ClientError
		if errors.As(err, &ce) {
			return 400, ce.Msg
		}
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s sobre
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	if data != nil && len(s.Data) > 0 {
		if err := json.Unmarshal(s.Data, data); err != nil {
			t.Fatalf("data: %v (%s)", err, s.Data)
		}
	}
	return resp.StatusCode, s.Message
}

type auth struct {
	ID          string         `json:"id"`
	Estado      string         `json:"estado"`
	SucursalID  string         `json:"sucursal_id"`
	Metadata    map[string]any `json:"metadata"`
	Solicitante map[string]any `json:"solicitante"`
	Aprobador   map[string]any `json:"aprobador"`
	CreatedAt   string         `json:"createdAt"`
	UpdatedAt   string         `json:"updatedAt"`
}

func solicitud(precio any) map[string]any {
	return map[string]any{
		"sucursal_id": "suc-1",
		"tipo":        "PRECIO_POS",
		"estado":      "APROBADA", // el cliente no puede fijar el estado
		"metadata":    map[string]any{"producto_id": "p1", "precio_solicitado": precio},
	}
}

func TestEstadosFiltro(t *testing.T) {
	got, err := estadosFiltro(" aprobada , RECHAZADA ")
	if err != nil || !reflect.DeepEqual(got, []string{Aprobada, Rechazada}) {
		t.Fatalf("got %v err %v", got, err)
	}
	if got, _ := estadosFiltro(""); !reflect.DeepEqual(got, []string{Pendiente}) {
		t.Fatalf("default = %v", got)
	}
	if _, err := estadosFiltro("BORRADA"); err == nil {
		t.Fatal("estado desconocido debía fallar")
	}
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	sinTenant := req("", solicitud(100))
	sinTenant.RequestContext.Authorizer = map[string]any{}
	if s, _ := llamar(t, Create, sinTenant, nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _ := llamar(t, Create, req("", map[string]any{"metadata": map[string]any{"precio_solicitado": 1}}), nil); s != 400 {
		t.Fatalf("sin sucursal: %d", s)
	}
	for _, precio := range []any{nil, "cien", -5} {
		if s, _ := llamar(t, Create, req("", solicitud(precio)), nil); s != 400 {
			t.Fatalf("precio %v: %d", precio, s)
		}
	}
	if s, _ := llamar(t, Update, req("[CAJERO]", map[string]string{"estado": Aprobada}), nil); s != 403 {
		t.Fatalf("cajero no debe aprobar: %d", s)
	}
	r := req("[ADMIN]", map[string]string{"estado": Aprobada})
	r.PathParameters = map[string]string{"id": "no-es-objectid"}
	if s, _ := llamar(t, Update, r, nil); s != 400 {
		t.Fatalf("id inválido: %d", s)
	}
}

func TestFlujoSolicitarAprobarYRechazar(t *testing.T) {
	c := testmongo.Conectar(t, "t_"+"11111111222233334444555555555555")

	var a auth
	if s, m := llamar(t, Create, req("[CAJERO]", solicitud(80.5)), &a); s != 201 {
		t.Fatalf("create: %d %s", s, m)
	}
	if a.Estado != Pendiente || a.ID == "" || a.Solicitante["nombre"] != "cajero@taller.com" || a.CreatedAt == "" {
		t.Fatalf("creada = %+v", a)
	}

	var b auth
	llamar(t, Create, req("[CAJERO]", solicitud(10)), &b)

	lista := func(estado string) []auth {
		r := req("[CAJERO]", nil)
		r.QueryStringParameters = map[string]string{"estado": estado, "sucursal_id": "suc-1"}
		var out struct{ Items []auth }
		if s, m := llamar(t, List, r, &out); s != 200 {
			t.Fatalf("list %s: %d %s", estado, s, m)
		}
		return out.Items
	}
	if n := len(lista("")); n != 2 {
		t.Fatalf("pendientes = %d", n)
	}

	resolver := func(id, estado, grupos string) int {
		r := req(grupos, map[string]string{"estado": estado})
		r.PathParameters = map[string]string{"id": id}
		s, _ := llamar(t, Update, r, nil)
		return s
	}
	if s := resolver(a.ID, Aprobada, "[ADMIN]"); s != 200 {
		t.Fatalf("aprobar: %d", s)
	}
	if s := resolver(a.ID, Rechazada, "[ADMIN]"); s != 404 {
		t.Fatalf("una resuelta no se vuelve a resolver: %d", s)
	}
	if s := resolver(b.ID, Rechazada, "SUPER_ADMIN"); s != 200 {
		t.Fatalf("rechazar: %d", s)
	}

	resueltas := lista("APROBADA,RECHAZADA")
	if len(resueltas) != 2 {
		t.Fatalf("resueltas = %d", len(resueltas))
	}
	porID := map[string]auth{}
	for _, x := range resueltas {
		porID[x.ID] = x
	}
	if porID[a.ID].Estado != Aprobada || porID[b.ID].Estado != Rechazada {
		t.Fatalf("estados = %+v", porID)
	}
	if porID[a.ID].Metadata["precio_solicitado"] != 80.5 || porID[a.ID].Aprobador["id"] != "sub-cajero" || porID[a.ID].UpdatedAt == "" {
		t.Fatalf("aprobada = %+v", porID[a.ID])
	}
	if len(lista("PENDIENTE")) != 0 {
		t.Fatal("no debían quedar pendientes")
	}

	// El estado del body se ignora al crear.
	n, _ := c.Database("t_11111111222233334444555555555555").Collection(coleccion).
		CountDocuments(context.Background(), bson.D{{Key: "estado", Value: Aprobada}})
	if n != 1 {
		t.Fatalf("aprobadas en BD = %d", n)
	}
}
