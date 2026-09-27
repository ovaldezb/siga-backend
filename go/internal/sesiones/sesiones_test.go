package sesiones

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const userSub = "sub-abc-123"

type sobre struct {
	Success bool           `json:"success"`
	Message string         `json:"message"`
	Data    map[string]any `json:"data"`
}

func leer(t *testing.T, resp platform.Response) sobre {
	t.Helper()
	var s sobre
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return s
}

func evento(sub, deviceID string, enBody bool) platform.Request {
	req := platform.Request{}
	req.RequestContext.Authorizer = map[string]any{"claims": map[string]any{
		"sub": sub, "email": "asesor@taller.com", "custom:tenant_id": "tenant-1",
	}}
	if deviceID != "" {
		if enBody {
			b, _ := json.Marshal(map[string]string{"device_id": deviceID})
			req.Body = string(b)
		} else {
			req.QueryStringParameters = map[string]string{"device_id": deviceID}
		}
	}
	return req
}

func setup(t *testing.T) *mongo.Collection {
	t.Helper()
	c := testmongo.Conectar(t, "_platform")
	indicesListos = false
	return c.Database("_platform").Collection(coleccion)
}

func llamar(t *testing.T, h platform.Handler, req platform.Request) (int, sobre) {
	t.Helper()
	resp, err := h(context.Background(), req)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	return resp.StatusCode, leer(t, resp)
}

func registrarDev(t *testing.T, device string) sobre {
	t.Helper()
	status, s := llamar(t, Registrar, evento(userSub, device, true))
	if status != 200 {
		t.Fatalf("registrar %s: status %d %s", device, status, s.Message)
	}
	return s
}

func envejecer(t *testing.T, col *mongo.Collection, device string, hace time.Duration) {
	t.Helper()
	_, err := col.UpdateOne(context.Background(), bson.D{{Key: "device_id", Value: device}},
		bson.D{{Key: "$set", Value: bson.D{{Key: "ultimo_acceso", Value: time.Now().UTC().Add(-hace)}}}})
	if err != nil {
		t.Fatal(err)
	}
}

func contar(t *testing.T, col *mongo.Collection, filtro bson.D) int64 {
	t.Helper()
	n, err := col.CountDocuments(context.Background(), filtro)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestDeviceIDObligatorio(t *testing.T) {
	for _, h := range []platform.Handler{Registrar, Estado, Cerrar} {
		if status, _ := llamar(t, h, evento(userSub, "", false)); status != 400 {
			t.Fatalf("sin device_id esperaba 400, dio %d", status)
		}
	}
}

func TestSinIdentidad401(t *testing.T) {
	if status, _ := llamar(t, Estado, evento("", "dev-1", false)); status != 401 {
		t.Fatalf("esperaba 401, dio %d", status)
	}
}

func TestSobrantes(t *testing.T) {
	otras := []sesion{{DeviceID: "a"}, {DeviceID: "b"}, {DeviceID: "c"}}
	got := sobrantes(otras)
	if len(got) != 2 || got[0].DeviceID != "b" || got[1].DeviceID != "c" {
		t.Fatalf("sobrantes = %+v", got)
	}
	if sobrantes(otras[:1]) != nil {
		t.Fatal("con una sola sesión ajena no se revoca nada")
	}
}

func TestDosSesionesConviven(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	s := registrarDev(t, "dev-2")
	if s.Data["sesiones_activas"] != float64(2) || s.Data["sesiones_cerradas"] != float64(0) {
		t.Fatalf("data = %v", s.Data)
	}
	if n := contar(t, col, bson.D{{Key: "revocada_en", Value: nil}}); n != 2 {
		t.Fatalf("vivas = %d", n)
	}
}

func TestTerceraRevocaLaMasAntigua(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	registrarDev(t, "dev-2")
	envejecer(t, col, "dev-1", 30*time.Minute)

	s := registrarDev(t, "dev-3")
	if s.Data["sesiones_cerradas"] != float64(1) || s.Data["sesiones_activas"] != float64(2) {
		t.Fatalf("data = %v", s.Data)
	}

	_, st := llamar(t, Estado, evento(userSub, "dev-1", false))
	if st.Data["vigente"] != false || st.Data["motivo"] != motivoLimite {
		t.Fatalf("dev-1 debía quedar revocado: %v", st.Data)
	}
	_, st = llamar(t, Estado, evento(userSub, "dev-2", false))
	if st.Data["vigente"] != true {
		t.Fatalf("dev-2 debía seguir vigente: %v", st.Data)
	}
}

func TestReentrarMismoDispositivoNoConsumeCupo(t *testing.T) {
	setup(t)
	registrarDev(t, "dev-1")
	registrarDev(t, "dev-2")
	s := registrarDev(t, "dev-1")
	if s.Data["sesiones_cerradas"] != float64(0) || s.Data["sesiones_activas"] != float64(2) {
		t.Fatalf("data = %v", s.Data)
	}
}

func TestSesionInactivaNoBloqueaCupo(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	registrarDev(t, "dev-2")
	envejecer(t, col, "dev-1", 13*time.Hour)

	s := registrarDev(t, "dev-3")
	if s.Data["sesiones_cerradas"] != float64(0) {
		t.Fatalf("la sesión inactiva no debía contar: %v", s.Data)
	}
}

// Regresión: un dispositivo dormido (>12 h sin latido) que vuelve a latir se
// refrescaba sin podar y el usuario quedaba con tres sesiones vivas.
func TestDispositivoDormidoAlVolverRespetaElLimite(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	envejecer(t, col, "dev-1", 13*time.Hour)
	registrarDev(t, "dev-2")
	envejecer(t, col, "dev-2", time.Minute)
	registrarDev(t, "dev-3")

	_, st := llamar(t, Estado, evento(userSub, "dev-1", false))
	if st.Data["vigente"] != true || st.Data["sesiones_activas"] != float64(2) {
		t.Fatalf("dev-1 vuelve como login nuevo: %v", st.Data)
	}
	vivasAhora, err := vivas(context.Background(), col, userSub, time.Now().UTC(), "")
	if err != nil {
		t.Fatal(err)
	}
	if len(vivasAhora) != MaxSesiones {
		t.Fatalf("vivas = %d, esperaba %d", len(vivasAhora), MaxSesiones)
	}
	_, st = llamar(t, Estado, evento(userSub, "dev-2", false))
	if st.Data["vigente"] != false {
		t.Fatalf("dev-2 era la más antigua y debía revocarse: %v", st.Data)
	}
}

func TestDispositivoDesconocidoSeDaDeAltaEnElLatido(t *testing.T) {
	col := setup(t)
	status, s := llamar(t, Estado, evento(userSub, "dev-viejo", false))
	if status != 200 || s.Data["vigente"] != true || s.Data["sesiones_activas"] != float64(1) {
		t.Fatalf("status %d data %v", status, s.Data)
	}
	if n := contar(t, col, bson.D{{Key: "device_id", Value: "dev-viejo"}}); n != 1 {
		t.Fatalf("no se registró el dispositivo")
	}
}

func TestLatidoRefrescaUltimoAcceso(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	envejecer(t, col, "dev-1", time.Hour)
	_, s := llamar(t, Estado, evento(userSub, "dev-1", false))
	if s.Message != "Sesión vigente" {
		t.Fatalf("mensaje = %q", s.Message)
	}
	var doc sesion
	if err := col.FindOne(context.Background(), bson.D{{Key: "device_id", Value: "dev-1"}}).Decode(&doc); err != nil {
		t.Fatal(err)
	}
	if time.Since(doc.UltimoAcceso) > time.Minute {
		t.Fatalf("ultimo_acceso no se refrescó: %v", doc.UltimoAcceso)
	}
}

func TestCerrarSesionLiberaElCupo(t *testing.T) {
	col := setup(t)
	registrarDev(t, "dev-1")
	registrarDev(t, "dev-2")
	if status, _ := llamar(t, Cerrar, evento(userSub, "dev-1", false)); status != 200 {
		t.Fatalf("cerrar status %d", status)
	}
	s := registrarDev(t, "dev-3")
	if s.Data["sesiones_cerradas"] != float64(0) {
		t.Fatalf("el cupo debía estar libre: %v", s.Data)
	}
	if n := contar(t, col, bson.D{{Key: "device_id", Value: "dev-1"}}); n != 0 {
		t.Fatal("dev-1 debía borrarse")
	}
}

func TestElLimiteEsPorUsuario(t *testing.T) {
	setup(t)
	registrarDev(t, "dev-1")
	registrarDev(t, "dev-2")
	_, s := llamar(t, Registrar, evento("otro-sub", "dev-9", true))
	if s.Data["sesiones_cerradas"] != float64(0) || s.Data["sesiones_activas"] != float64(1) {
		t.Fatalf("data = %v", s.Data)
	}
}
