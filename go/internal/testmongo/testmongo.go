// Package testmongo conecta las pruebas de integración a un Mongo desechable
// indicado en SIGA_MONGO_TEST_URI; sin la variable, las pruebas se saltan.
// Nunca apuntarlo a un cluster real: al terminar se borran las bases que tocan.
package testmongo

import (
	"context"
	"os"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// Conectar deja listo platform.Client y borra `dbs` antes y después de la prueba.
func Conectar(t *testing.T, dbs ...string) *mongo.Client {
	t.Helper()
	uri := os.Getenv("SIGA_MONGO_TEST_URI")
	if uri == "" {
		t.Skip("SIGA_MONGO_TEST_URI no definido: se omite la prueba de integración")
	}
	c, err := mongo.Connect(options.Client().ApplyURI(uri).SetServerSelectionTimeout(5 * time.Second))
	if err != nil {
		t.Fatalf("no se pudo conectar a %s: %v", uri, err)
	}
	limpiar := func() {
		for _, db := range dbs {
			_ = c.Database(db).Drop(context.Background())
		}
	}
	limpiar()
	t.Cleanup(limpiar)
	platform.SetClientForTests(c)
	return c
}
