package platform

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"

	"github.com/aws/aws-lambda-go/events"
)

// Request/Response son el formato 1.0 del httpApi (provider.httpApi.payload: '1.0').
type (
	Request  = events.APIGatewayProxyRequest
	Response = events.APIGatewayProxyResponse
)

type envelope struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
	Data    any    `json:"data"`
}

// JSON arma el sobre {success, message, data} con los mismos headers que
// create_response de Python.
func JSON(req Request, status int, message string, data any) Response {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(envelope{Success: status < 400, Message: message, Data: data}); err != nil {
		// Solo pasa con tipos no serializables: es un bug del handler, no del cliente.
		logger.Error("no se pudo serializar la respuesta", "error", err)
		status = 500
		buf.Reset()
		_ = json.NewEncoder(&buf).Encode(envelope{Message: msgErrorInterno})
	}

	headers := map[string]string{
		"Content-Type":                 "application/json",
		"Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS,PATCH",
		"Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
	}
	for k, v := range corsHeaders(req) {
		headers[k] = v
	}

	return Response{
		StatusCode: status,
		Headers:    headers,
		Body:       strings.TrimSuffix(buf.String(), "\n"),
	}
}

// corsHeaders: con ALLOWED_ORIGINS se hace echo del Origin permitido y se habilitan
// credentials; sin allowlist, comodín sin credentials (la combinación es inválida).
func corsHeaders(req Request) map[string]string {
	var allowed []string
	for _, o := range strings.Split(os.Getenv("ALLOWED_ORIGINS"), ",") {
		if o = strings.TrimSpace(o); o != "" {
			allowed = append(allowed, o)
		}
	}
	if len(allowed) == 0 {
		return map[string]string{"Access-Control-Allow-Origin": "*"}
	}

	origin := req.Headers["Origin"]
	if origin == "" {
		origin = req.Headers["origin"]
	}
	chosen := allowed[0]
	for _, o := range allowed {
		if o == origin {
			chosen = origin
			break
		}
	}
	return map[string]string{
		"Access-Control-Allow-Origin":      chosen,
		"Access-Control-Allow-Credentials": "true",
		"Vary":                             "Origin",
	}
}
