// Package catalogos porta a Go los catálogos de solo lectura de `_platform`:
// búsqueda SAT (sat_manager.py) y marcas/modelos de autos (autos_manager.py).
package catalogos

import (
	"context"
	"regexp"
	"strings"
	"unicode/utf8"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// satCatalogo describe cómo buscar en cada catálogo SAT y cómo exponer sus campos.
type satCatalogo struct {
	coleccion string
	// sinTermino: el catálogo es corto y se lista completo aunque no haya `q`.
	sinTermino bool
	limite     int64
	// campos: nombre en la respuesta -> campo en Mongo.
	campos [][2]string
}

var satCatalogos = map[string]satCatalogo{
	"unidad": {
		coleccion: "unidad", limite: 50,
		campos: [][2]string{{"clave", "clave"}, {"descripcion", "descripcion"}},
	},
	"clavesat": {
		coleccion: "catprodserv", limite: 50,
		campos: [][2]string{{"clave", "clave"}, {"descripcion", "descripcion"}},
	},
	"regimenfiscal": {
		coleccion: "regimen_fiscal", sinTermino: true, limite: 100,
		campos: [][2]string{{"clave", "regimenfiscal"}, {"descripcion", "descripcion"}, {"fisica", "fisica"}, {"moral", "moral"}},
	},
	"usocfdi": {
		coleccion: "usocfdi", sinTermino: true, limite: 100,
		campos: [][2]string{{"clave", "usoCfdi"}, {"descripcion", "descripcion"}, {"regfiscalreceptor", "regfiscalreceptor"}, {"fisica", "fisica"}, {"moral", "moral"}},
	},
}

// SearchSAT atiende GET /catalogos/sat/{tipoBusqueda}?q=…
func SearchSAT(ctx context.Context, req platform.Request) (platform.Response, error) {
	tipo := req.PathParameters["tipoBusqueda"]
	cat, ok := satCatalogos[tipo]
	if !ok {
		return platform.JSON(req, 400, "Tipo de búsqueda inválido. Debe ser 'unidad', 'clavesat', 'regimenfiscal' o 'usocfdi'.", nil), nil
	}

	q := strings.TrimSpace(req.QueryStringParameters["q"])
	if !cat.sinTermino && utf8.RuneCountInString(q) < 2 {
		return platform.JSON(req, 200, "Búsqueda vacía", []any{}), nil
	}

	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}

	opts := options.Find().SetProjection(cat.proyeccion()).SetLimit(cat.limite)
	cur, err := db.Collection(cat.coleccion).Find(ctx, satFiltro(tipo, q), opts)
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	results := make([]map[string]any, 0, len(docs))
	for _, doc := range docs {
		results = append(results, cat.mapear(doc))
	}

	platform.Logger().Info("SAT search", "tipo", tipo, "q", q, "resultados", len(results))
	return platform.JSON(req, 200, "Resultados de búsqueda SAT", results), nil
}

// satFiltro: unidades se anclan al inicio de descripción o clave; claves de
// producto buscan por substring; régimen y uso CFDI filtran la descripción solo
// si hay término.
//
// Diferencia deliberada con Python: allí el término de régimen/uso CFDI iba sin
// escapar, así que un "(" rompía la regex en Mongo y devolvía 500.
func satFiltro(tipo, q string) bson.D {
	escaped := regexp.QuoteMeta(q)
	switch tipo {
	case "regimenfiscal", "usocfdi":
		if q == "" {
			return bson.D{}
		}
		return bson.D{{Key: "descripcion", Value: bson.Regex{Pattern: escaped, Options: "i"}}}
	case "unidad":
		escaped = "^" + escaped
	}
	return bson.D{{Key: "$or", Value: bson.A{
		bson.D{{Key: "descripcion", Value: bson.Regex{Pattern: escaped, Options: "i"}}},
		bson.D{{Key: "clave", Value: bson.Regex{Pattern: escaped, Options: "i"}}},
	}}}
}

func (c satCatalogo) proyeccion() bson.D {
	p := bson.D{{Key: "_id", Value: 0}}
	for _, campo := range c.campos {
		p = append(p, bson.E{Key: campo[1], Value: 1})
	}
	return p
}

// mapear deja en null los campos ausentes, igual que doc.get() en Python.
func (c satCatalogo) mapear(doc bson.M) map[string]any {
	out := make(map[string]any, len(c.campos))
	for _, campo := range c.campos {
		out[campo[0]] = doc[campo[1]]
	}
	return out
}
