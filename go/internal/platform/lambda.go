package platform

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/getsentry/sentry-go"
)

const msgErrorInterno = "Ha ocurrido un error interno en el servidor."

var logger = slog.New(slog.NewJSONHandler(os.Stdout, nil)).
	With("service", "siga-backend", "function", os.Getenv("AWS_LAMBDA_FUNCTION_NAME"))

// Logger es el logger JSON compartido (equivalente al Logger de powertools).
func Logger() *slog.Logger { return logger }

// ClientError marca un error atribuible a la petición: se responde 400 y no se
// reporta a Sentry, como KeyError/ValueError en handle_exception.
type ClientError struct{ Msg string }

func (e *ClientError) Error() string { return e.Msg }

// BadRequest crea un ClientError.
func BadRequest(format string, args ...any) error {
	return &ClientError{Msg: fmt.Sprintf(format, args...)}
}

// Handler es la firma de las lambdas: devuelven una respuesta o un error, y Start
// traduce el error al mismo sobre que usa handle_exception.
type Handler func(ctx context.Context, req Request) (Response, error)

var sentryEnabled = initSentry()

func initSentry() bool {
	dsn := strings.TrimSpace(os.Getenv("SENTRY_DSN"))
	if dsn == "" {
		return false
	}
	env := os.Getenv("SENTRY_ENVIRONMENT")
	if env == "" {
		env = os.Getenv("STAGE")
	}
	if env == "" {
		env = "dev"
	}
	rate, _ := strconv.ParseFloat(os.Getenv("SENTRY_TRACES_SAMPLE_RATE"), 64)
	err := sentry.Init(sentry.ClientOptions{
		Dsn:              dsn,
		Environment:      env,
		TracesSampleRate: rate,
		SendDefaultPII:   false,
	})
	if err != nil {
		logger.Warn("no se pudo inicializar Sentry", "error", err)
		return false
	}
	return true
}

// Start arranca la lambda con manejo uniforme de errores y panics.
func Start(h Handler) {
	lambda.Start(func(ctx context.Context, req Request) (resp Response, err error) {
		defer func() {
			if r := recover(); r != nil {
				resp = handleError(req, fmt.Errorf("panic: %v", r))
				err = nil
			}
		}()
		resp, err = h(ctx, req)
		if err != nil {
			return handleError(req, err), nil
		}
		return resp, nil
	})
}

func handleError(req Request, err error) Response {
	var ce *ClientError
	if errors.As(err, &ce) {
		logger.Warn("client error", "error", err)
		return JSON(req, 400, "Solicitud inválida: "+ce.Msg, nil)
	}

	logger.Error("an error occurred", "error", err, "path", req.Path)
	captureError(req, err)
	return JSON(req, 500, msgErrorInterno, nil)
}

// captureError replica capture_with_event: tags de tenant, usuario y ruta. Se
// hace Flush porque Lambda congela el contenedor al responder.
func captureError(req Request, err error) {
	if !sentryEnabled {
		return
	}
	claims := Claims(req)
	sentry.WithScope(func(scope *sentry.Scope) {
		if t, _ := claims["custom:tenant_id"].(string); t != "" {
			scope.SetTag("tenant_id", t)
		}
		if e, _ := claims["email"].(string); e != "" {
			scope.SetUser(sentry.User{Email: e})
		}
		path := req.Path
		if path == "" {
			path = req.Resource
		}
		if path != "" {
			scope.SetTag("api.path", path)
		}
		sentry.CaptureException(err)
	})
	sentry.Flush(2 * time.Second)
}

// Claims devuelve los claims del JWT (httpApi) o del authorizer REST.
func Claims(req Request) map[string]any {
	auth := req.RequestContext.Authorizer
	if auth == nil {
		return map[string]any{}
	}
	if jwt, ok := auth["jwt"].(map[string]any); ok {
		if c, ok := jwt["claims"].(map[string]any); ok {
			return c
		}
		return map[string]any{}
	}
	if c, ok := auth["claims"].(map[string]any); ok {
		return c
	}
	return map[string]any{}
}
