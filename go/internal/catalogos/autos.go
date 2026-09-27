package catalogos

import (
	"context"
	"regexp"
	"sort"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// ListMarcas atiende GET /catalogos/autos/marcas: une el catálogo administrable
// (`_platform.marcas` activas) con las marcas legacy escritas en vehículos, para
// que ningún selector se quede vacío. El alta/edición sigue en autos_manager.py.
func ListMarcas(ctx context.Context, req platform.Request) (platform.Response, error) {
	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}

	cur, err := db.Collection("marcas").Find(ctx, bson.D{{Key: "activa", Value: true}},
		options.Find().SetProjection(bson.D{{Key: "nombre", Value: 1}}))
	if err != nil {
		return platform.Response{}, err
	}
	var activas []bson.M
	if err := cur.All(ctx, &activas); err != nil {
		return platform.Response{}, err
	}

	legacy, err := distinctStrings(ctx, db.Collection("vehiculos"), "marca", bson.D{})
	if err != nil {
		return platform.Response{}, err
	}

	nombres := make([]string, 0, len(activas)+len(legacy))
	for _, m := range activas {
		if n, _ := m["nombre"].(string); n != "" {
			nombres = append(nombres, n)
		}
	}
	for _, n := range legacy {
		if n != "" {
			nombres = append(nombres, n)
		}
	}

	return platform.JSON(req, 200, "Marcas recuperadas", sortedUnique(nombres)), nil
}

// ListModelos atiende GET /catalogos/autos/modelos?marca=…: modelos únicos de
// vehículos cuya marca coincide completa, sin distinguir mayúsculas.
func ListModelos(ctx context.Context, req platform.Request) (platform.Response, error) {
	marca := req.QueryStringParameters["marca"]
	if marca == "" {
		return platform.JSON(req, 400, "El parámetro 'marca' es obligatorio", nil), nil
	}

	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}

	modelos, err := distinctStrings(ctx, db.Collection("vehiculos"), "modelo", marcaFiltro(marca))
	if err != nil {
		return platform.Response{}, err
	}

	return platform.JSON(req, 200, "Modelos para "+marca+" recuperados", sortedUnique(modelos)), nil
}

func marcaFiltro(marca string) bson.D {
	return bson.D{{Key: "marca", Value: bson.Regex{Pattern: "^" + regexp.QuoteMeta(marca) + "$", Options: "i"}}}
}

// distinctStrings descarta valores no-string (null, números): en Python mezclarlos
// hacía fallar sorted() con 500.
func distinctStrings(ctx context.Context, col *mongo.Collection, campo string, filtro bson.D) ([]string, error) {
	var valores []any
	if err := col.Distinct(ctx, campo, filtro).Decode(&valores); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(valores))
	for _, v := range valores {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out, nil
}

// sortedUnique ordena por punto de código, como sorted() de Python (el orden de
// bytes UTF-8 coincide con el de code points).
func sortedUnique(xs []string) []string {
	sort.Strings(xs)
	out := make([]string, 0, len(xs))
	for i, x := range xs {
		if i == 0 || x != xs[i-1] {
			out = append(out, x)
		}
	}
	return out
}
