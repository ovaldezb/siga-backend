import os
import re

def migrate_serverless_yml():
    filepath = "serverless.yml"
    if not os.path.exists(filepath):
        print(f"Error: {filepath} no existe.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    in_http_api_event = False
    in_authorizer = False
    spaces_event = 0
    spaces_authorizer = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Detectar el inicio de un evento http y cambiarlo a httpApi
        if re.match(r'^\s*-\s+http\s*:', line):
            line = line.replace("- http:", "- httpApi:")
            in_http_api_event = True
            in_authorizer = False
            spaces_event = indent
            new_lines.append(line)
            i += 1
            continue

        # Si estamos dentro del bloque de un evento httpApi
        if in_http_api_event:
            # Si la indentación vuelve al nivel del evento o menor, salimos del bloque
            if indent <= spaces_event and stripped and not stripped.startswith("-"):
                in_http_api_event = False
                in_authorizer = False
            else:
                # Omitir cors: true o cualquier configuración de cors en httpApi
                if stripped.startswith("cors:"):
                    i += 1
                    continue
                # Manejar el autorizador
                if stripped.startswith("authorizer:"):
                    in_authorizer = True
                    spaces_authorizer = indent
                    new_lines.append(line)
                    i += 1
                    continue

                if in_authorizer:
                    if indent <= spaces_authorizer and stripped:
                        in_authorizer = False
                    else:
                        # Omitir type y arn del authorizer en httpApi
                        if stripped.startswith("type:") or stripped.startswith("arn:"):
                            i += 1
                            continue

        new_lines.append(line)
        i += 1

    # Escribir el archivo modificado
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(new_lines)
    print("serverless.yml migrado exitosamente a httpApi.")


def patch_python_files():
    src_dir = "src"
    if not os.path.exists(src_dir):
        print(f"Error: la carpeta {src_dir} no existe.")
        return

    # Regex para detectar los accesos directos a claims
    # Detecta patrones como:
    # event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    # (event or {}).get("requestContext", {}).get("authorizer", {}).get("claims", {})
    # event.get('requestContext', {}).get('authorizer', {}).get('claims', {}) or {}
    pattern = re.compile(
        r'\(?\s*(?:event|event\s+or\s+\{\})\s*\)?'
        r'\.get\(\s*[\'"]requestContext[\'"]\s*,\s*\{\}\s*\)'
        r'\.get\(\s*[\'"]authorizer[\'"]\s*,\s*\{\}\s*\)'
        r'\.get\(\s*[\'"]claims[\'"]\s*,\s*\{\}\s*\)'
    )

    modified_count = 0

    for root, _, files in os.walk(src_dir):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file)
            
            # Evitar auto-modificarse a sí mismo o modificar auth_utils que contiene la definición
            if file == "auth_utils.py":
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if pattern.search(content):
                print(f"Parcheando accesos a claims en: {filepath}")
                
                # Reemplazar todos los accesos con get_claims(event)
                # O si incluye el 'or {}', simplificarlo
                new_content = pattern.sub("get_claims(event)", content)
                
                # Normalizar get_claims(event) or {} a get_claims(event) ya que get_claims siempre devuelve un dict
                new_content = new_content.replace("get_claims(event) or {}", "get_claims(event)")
                new_content = new_content.replace("get_claims(event or {})", "get_claims(event)")

                # Asegurar el import al inicio del archivo
                import_line = "from src.shared.utils.auth_utils import get_claims"
                
                # Si ya tiene una importación de auth_utils, agregar get_claims
                if "from src.shared.utils.auth_utils import" in new_content:
                    # Encontrar la línea de importación
                    import_pattern = re.compile(r'(from\s+src\.shared\.utils\.auth_utils\s+import\s+)(.+)')
                    match = import_pattern.search(new_content)
                    if match:
                        imported_funcs = match.group(2).strip()
                        if "get_claims" not in imported_funcs:
                            # Si es un import multilínea con paréntesis
                            if imported_funcs.startswith("("):
                                # Insertar antes del cierre de paréntesis
                                new_funcs = imported_funcs.replace(")", ", get_claims)")
                                new_content = new_content.replace(match.group(0), f"{match.group(1)}{new_funcs}")
                            else:
                                new_content = new_content.replace(match.group(0), f"{match.group(1)}{imported_funcs}, get_claims")
                else:
                    # Si no tiene importación, insertarla al inicio del archivo
                    # Preferiblemente después de __future__ imports o al principio
                    lines = new_content.splitlines()
                    insert_idx = 0
                    for idx, l in enumerate(lines):
                        if l.startswith("from __future__") or l.startswith('"""') or l.startswith("'''"):
                            insert_idx = idx + 1
                            continue
                        break
                    lines.insert(insert_idx, import_line)
                    new_content = "\n".join(lines)

                with open(filepath, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
                modified_count += 1

    print(f"Modificados {modified_count} archivos de Python exitosamente.")

if __name__ == "__main__":
    print("Iniciando migración automatizada a HTTP API...")
    migrate_serverless_yml()
    patch_python_files()
    print("Migración completada con éxito.")
