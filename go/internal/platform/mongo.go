// Package platform es la base común de las lambdas Go de siga-backend: el
// equivalente de src/shared (database.py, response_handler.py, sentry_init.py).
package platform

import (
	"fmt"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"
)

var (
	clientOnce sync.Once
	client     *mongo.Client
	clientErr  error
)

// Client devuelve el cliente Mongo compartido entre invocaciones del mismo
// contenedor. Igual que database.py: sin ping al arrancar (el driver conecta en
// la primera operación), pool chico y timeouts cortos para fallar rápido.
func Client() (*mongo.Client, error) {
	clientOnce.Do(func() {
		uri, err := mongoURI()
		if err != nil {
			clientErr = err
			return
		}
		client, clientErr = mongo.Connect(options.Client().
			ApplyURI(uri).
			SetMaxPoolSize(10).
			SetMinPoolSize(0).
			SetServerSelectionTimeout(5 * time.Second).
			SetConnectTimeout(5 * time.Second).
			SetTimeout(20 * time.Second).
			SetRetryWrites(true))
	})
	return client, clientErr
}

func mongoURI() (string, error) {
	user := os.Getenv("MONGO_USER")
	password := os.Getenv("MONGO_PASSWORD")
	host := os.Getenv("MONGO_HOST")
	dbName := os.Getenv("MONGO_DB")
	if dbName == "" {
		dbName = "siga"
	}

	var missing []string
	for k, v := range map[string]string{"MONGO_USER": user, "MONGO_PASSWORD": password, "MONGO_HOST": host} {
		if v == "" {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		return "", fmt.Errorf("faltan componentes de conexión a MongoDB en variables de entorno: %s", strings.Join(missing, ", "))
	}

	// url.URL escapa usuario y password; el f-string de Python no lo hace, pero
	// para credenciales sin caracteres especiales el resultado es idéntico.
	u := url.URL{
		Scheme:   "mongodb+srv",
		User:     url.UserPassword(user, password),
		Host:     host,
		Path:     "/" + dbName,
		RawQuery: "retryWrites=true&w=majority",
	}
	return u.String(), nil
}

// PlatformDB es la base global `_platform` (catálogos, talleres, sesiones).
func PlatformDB() (*mongo.Database, error) {
	c, err := Client()
	if err != nil {
		return nil, err
	}
	return c.Database("_platform"), nil
}

// TenantDB replica get_tenant_db: Atlas limita el nombre a 38 bytes, así que se
// quitan los guiones del UUID y se usa el prefijo corto "t_".
func TenantDB(tenantID string) (*mongo.Database, error) {
	if tenantID == "" {
		return nil, fmt.Errorf("tenant_id must be provided to get tenant database")
	}
	c, err := Client()
	if err != nil {
		return nil, err
	}
	return c.Database("t_" + strings.ReplaceAll(tenantID, "-", "")), nil
}

// SetClientForTests sustituye el cliente compartido; solo para pruebas de
// integración contra un Mongo desechable (ver internal/testmongo).
func SetClientForTests(c *mongo.Client) {
	clientOnce.Do(func() {})
	client, clientErr = c, nil
}
