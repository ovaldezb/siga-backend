package catalogos

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
)

type sobre struct {
	Success bool            `json:"success"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func leer(t *testing.T, resp platform.Response) sobre {
	t.Helper()
	var s sobre
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return s
}

func TestSearchSATTipoInvalido(t *testing.T) {
	resp, err := SearchSAT(context.Background(), platform.Request{PathParameters: map[string]string{"tipoBusqueda": "otro"}})
	if err != nil {
		t.Fatal(err)
	}
	if s := leer(t, resp); resp.StatusCode != 400 || s.Success {
		t.Fatalf("esperaba 400 success=false, got %d %+v", resp.StatusCode, s)
	}
}

func TestSearchSATTerminoCortoNoConsulta(t *testing.T) {
	// Sin variables de Mongo: si intentara consultar devolvería error.
	for _, tipo := range []string{"unidad", "clavesat"} {
		resp, err := SearchSAT(context.Background(), platform.Request{
			PathParameters:        map[string]string{"tipoBusqueda": tipo},
			QueryStringParameters: map[string]string{"q": " a "},
		})
		if err != nil {
			t.Fatalf("%s: %v", tipo, err)
		}
		s := leer(t, resp)
		if resp.StatusCode != 200 || s.Message != "Búsqueda vacía" || string(s.Data) != "[]" {
			t.Fatalf("%s: got %d %+v data=%s", tipo, resp.StatusCode, s, s.Data)
		}
	}
}

func TestSatFiltro(t *testing.T) {
	re := func(p string) bson.Regex { return bson.Regex{Pattern: p, Options: "i"} }
	casos := []struct {
		tipo, q string
		want    bson.D
	}{
		{"unidad", "pz.", bson.D{{Key: "$or", Value: bson.A{
			bson.D{{Key: "descripcion", Value: re(`^pz\.`)}},
			bson.D{{Key: "clave", Value: re(`^pz\.`)}},
		}}}},
		{"clavesat", "aceite", bson.D{{Key: "$or", Value: bson.A{
			bson.D{{Key: "descripcion", Value: re("aceite")}},
			bson.D{{Key: "clave", Value: re("aceite")}},
		}}}},
		{"regimenfiscal", "", bson.D{}},
		{"usocfdi", "gastos (", bson.D{{Key: "descripcion", Value: re(`gastos \(`)}}},
	}
	for _, c := range casos {
		if got := satFiltro(c.tipo, c.q); !reflect.DeepEqual(got, c.want) {
			t.Errorf("satFiltro(%q,%q) = %v, want %v", c.tipo, c.q, got, c.want)
		}
	}
}

func TestSatMapeo(t *testing.T) {
	got := satCatalogos["regimenfiscal"].mapear(bson.M{"regimenfiscal": "601", "descripcion": "General", "fisica": "No"})
	want := map[string]any{"clave": "601", "descripcion": "General", "fisica": "No", "moral": nil}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	if p := satCatalogos["usocfdi"].proyeccion(); p[0].Key != "_id" || len(p) != 6 {
		t.Fatalf("proyección inesperada: %v", p)
	}
}

func TestListModelosSinMarca(t *testing.T) {
	resp, err := ListModelos(context.Background(), platform.Request{})
	if err != nil {
		t.Fatal(err)
	}
	if s := leer(t, resp); resp.StatusCode != 400 || s.Message != "El parámetro 'marca' es obligatorio" {
		t.Fatalf("got %d %+v", resp.StatusCode, s)
	}
}

func TestMarcaFiltroEscapa(t *testing.T) {
	want := bson.D{{Key: "marca", Value: bson.Regex{Pattern: `^Mercedes\.Benz$`, Options: "i"}}}
	if got := marcaFiltro("Mercedes.Benz"); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}

func TestSortedUnique(t *testing.T) {
	got := sortedUnique([]string{"Nissan", "Audi", "Škoda", "Nissan", "BMW"})
	want := []string{"Audi", "BMW", "Nissan", "Škoda"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}
