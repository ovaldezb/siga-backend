package platform

import (
	"errors"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"
)

func TestJSONSobreYCors(t *testing.T) {
	t.Setenv("ALLOWED_ORIGINS", "")
	resp := JSON(Request{}, 201, "Creado <ok>", map[string]any{"a": 1})
	if resp.Body != `{"success":true,"message":"Creado <ok>","data":{"a":1}}` {
		t.Fatalf("body: %s", resp.Body)
	}
	if resp.Headers["Access-Control-Allow-Origin"] != "*" || resp.Headers["Access-Control-Allow-Credentials"] != "" {
		t.Fatalf("headers: %v", resp.Headers)
	}
	if b := JSON(Request{}, 404, "x", nil).Body; !strings.HasPrefix(b, `{"success":false`) || !strings.HasSuffix(b, `"data":null}`) {
		t.Fatalf("body 404: %s", b)
	}
}

func TestCorsAllowlist(t *testing.T) {
	t.Setenv("ALLOWED_ORIGINS", "https://a.mx, https://b.mx")
	h := JSON(Request{Headers: map[string]string{"origin": "https://b.mx"}}, 200, "", nil).Headers
	if h["Access-Control-Allow-Origin"] != "https://b.mx" || h["Access-Control-Allow-Credentials"] != "true" {
		t.Fatalf("headers: %v", h)
	}
	h = JSON(Request{Headers: map[string]string{"Origin": "https://evil"}}, 200, "", nil).Headers
	if h["Access-Control-Allow-Origin"] != "https://a.mx" {
		t.Fatalf("headers: %v", h)
	}
}

func TestHandleError(t *testing.T) {
	if r := handleError(Request{}, BadRequest("falta %s", "x")); r.StatusCode != 400 || !strings.Contains(r.Body, "Solicitud inválida: falta x") {
		t.Fatalf("400: %d %s", r.StatusCode, r.Body)
	}
	if r := handleError(Request{}, errors.New("boom")); r.StatusCode != 500 || !strings.Contains(r.Body, msgErrorInterno) {
		t.Fatalf("500: %d %s", r.StatusCode, r.Body)
	}
}

func TestClaimsJWT(t *testing.T) {
	req := Request{RequestContext: events.APIGatewayProxyRequestContext{Authorizer: map[string]any{
		"jwt": map[string]any{"claims": map[string]any{"custom:tenant_id": "t1"}},
	}}}
	if Claims(req)["custom:tenant_id"] != "t1" {
		t.Fatal("no leyó claims jwt")
	}
	if len(Claims(Request{})) != 0 {
		t.Fatal("sin authorizer debe ser vacío")
	}
}

func TestMongoURI(t *testing.T) {
	t.Setenv("MONGO_USER", "u")
	t.Setenv("MONGO_PASSWORD", "p@ss")
	t.Setenv("MONGO_HOST", "c0.mongodb.net")
	t.Setenv("MONGO_DB", "")
	uri, err := mongoURI()
	if err != nil || uri != "mongodb+srv://u:p%40ss@c0.mongodb.net/siga?retryWrites=true&w=majority" {
		t.Fatalf("uri=%s err=%v", uri, err)
	}
	t.Setenv("MONGO_HOST", "")
	if _, err := mongoURI(); err == nil || !strings.Contains(err.Error(), "MONGO_HOST") {
		t.Fatalf("esperaba error por MONGO_HOST: %v", err)
	}
}

func TestTenantDBSinTenant(t *testing.T) {
	if _, err := TenantDB(""); err == nil {
		t.Fatal("TenantDB vacío debe fallar")
	}
}
