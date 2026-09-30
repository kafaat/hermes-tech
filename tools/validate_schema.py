"""Shared schema check for tools that must refuse invalid output (uses validate.MiniValidator logic)."""
import json, re


def errors_for(instance, schema):
    try:
        import jsonschema  # type: ignore
        return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(instance)]
    except ImportError:
        pass
    out = []

    def walk(inst, sch, path):
        if "enum" in sch and inst not in sch["enum"]:
            out.append(f"{path}: not in enum"); return
        t = sch.get("type")
        types = {"object": dict, "array": list, "string": str, "boolean": bool}
        if t == "integer" and not (isinstance(inst, int) and not isinstance(inst, bool)):
            out.append(f"{path}: not integer"); return
        if t == "number" and not (isinstance(inst, (int, float)) and not isinstance(inst, bool)):
            out.append(f"{path}: not number"); return
        if t in types and not isinstance(inst, types[t]):
            out.append(f"{path}: not {t}"); return
        if isinstance(inst, str) and "pattern" in sch and not re.search(sch["pattern"], inst):
            out.append(f"{path}: pattern")
        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in sch and inst < sch["minimum"]: out.append(f"{path}: < min")
            if "maximum" in sch and inst > sch["maximum"]: out.append(f"{path}: > max")
        if isinstance(inst, dict):
            for r in sch.get("required", []):
                if r not in inst: out.append(f"{path}: missing {r}")
            for k, v in inst.items():
                if k in sch.get("properties", {}):
                    walk(v, sch["properties"][k], f"{path}.{k}")
                elif sch.get("additionalProperties") is False:
                    out.append(f"{path}: unexpected {k}")
        if isinstance(inst, list) and "items" in sch:
            for i, it in enumerate(inst):
                walk(it, sch["items"], f"{path}[{i}]")
    walk(instance, schema, "$")
    return out


def free_text_fields(schema, path="$"):
    """Every string field must be closed by enum, const or pattern; returns the open ones."""
    found = []
    if schema.get("type") == "string" and not any(k in schema for k in ("enum", "const", "pattern")):
        found.append(path)
    for k, v in schema.get("properties", {}).items():
        found += free_text_fields(v, f"{path}.{k}")
    if isinstance(schema.get("items"), dict):
        found += free_text_fields(schema["items"], f"{path}[]")
    return found
