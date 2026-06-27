import os
import re

def fix_paths():
    filepath = "serverless.yml"
    if not os.path.exists(filepath):
        print(f"Error: {filepath} no existe.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Expresión regular para encontrar "path: <algo>" que no empiece con "/"
    # Captura la indentación y el nombre del path.
    # Evita modificar variables de entorno o cosas que no sean rutas directas de httpApi.
    # Usaremos una aproximación línea por línea para seguridad absoluta.
    lines = content.splitlines()
    new_lines = []
    modified_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("path:"):
            # Extraer el valor del path
            parts = stripped.split("path:", 1)
            path_val = parts[1].strip()
            # Remover comillas si existen para verificar y agregar la barra
            quote_char = ""
            if path_val.startswith("'") and path_val.endswith("'"):
                quote_char = "'"
                path_val = path_val[1:-1]
            elif path_val.startswith('"') and path_val.endswith('"'):
                quote_char = '"'
                path_val = path_val[1:-1]

            if not path_val.startswith("/") and not path_val.startswith("${"):
                path_val = "/" + path_val
                # Reconstruir la línea con la misma indentación
                indent = len(line) - len(line.lstrip())
                new_line = " " * indent + f"path: {quote_char}{path_val}{quote_char}"
                new_lines.append(new_line)
                modified_count += 1
                continue

        # Desactivar temporalmente dockerizePip para validación local
        if stripped.startswith("dockerizePip:"):
            indent = len(line) - len(line.lstrip())
            new_lines.append(" " * indent + "dockerizePip: false")
            continue

        new_lines.append(line)

    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"Corregidos {modified_count} paths en serverless.yml.")

if __name__ == "__main__":
    fix_paths()
