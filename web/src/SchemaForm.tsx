import { useEffect, useRef, type TextareaHTMLAttributes } from "react";
import type { Schema } from "./api";

function fieldLabel(schema: Schema, path: string): string {
  if (schema.title) return schema.title;
  const name = (path.split(".").pop() || "input")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ");
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function ArrayField({
  value,
  onValue,
  ...props
}: Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> & {
  value: unknown;
  onValue: (value: unknown) => void;
}) {
  const element = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    element.current?.setCustomValidity(
      value !== undefined && !Array.isArray(value)
        ? "Enter a valid JSON array."
        : "",
    );
  }, [value]);
  return (
    <textarea
      {...props}
      ref={element}
      className="code"
      value={
        typeof value === "string" ? value : JSON.stringify(value ?? [], null, 2)
      }
      onChange={(e) => {
        const raw = e.target.value;
        if (!raw && !props.required) {
          onValue(undefined);
          return;
        }
        try {
          const parsed = JSON.parse(raw);
          onValue(Array.isArray(parsed) ? parsed : raw);
        } catch {
          onValue(raw);
        }
      }}
    />
  );
}

export function SchemaForm({
  schema,
  value,
  onChange,
  prefix = "input",
  required = false,
}: {
  schema: Schema;
  value: unknown;
  onChange: (value: unknown) => void;
  prefix?: string;
  required?: boolean;
}) {
  const title = fieldLabel(schema, prefix);
  if (schema.type === "object") {
    const object = (value || {}) as Record<string, unknown>;
    const optional = prefix !== "input" && !required;
    return (
      <div className="fields">
        {prefix !== "input" &&
          (optional ? (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={value !== undefined}
                onChange={(e) => onChange(e.target.checked ? {} : undefined)}
              />
              Include {title}
            </label>
          ) : (
            <span className="object-label">{title} *</span>
          ))}
        {(!optional || value !== undefined) &&
          Object.entries(schema.properties || {}).map(([name, child]) => (
            <div key={name}>
              <SchemaForm
                schema={child}
                required={schema.required?.includes(name)}
                value={object[name]}
                prefix={prefix + "." + name}
                onChange={(next) => onChange({ ...object, [name]: next })}
              />
            </div>
          ))}
      </div>
    );
  }
  const label = title + (required ? " *" : "");
  const shared = {
    id: prefix,
    "aria-describedby": schema.description ? prefix + "-hint" : undefined,
  };
  return (
    <div className="field">
      <label htmlFor={prefix}>{label}</label>
      {schema.description && (
        <span className="hint" id={prefix + "-hint"}>
          {schema.description}
        </span>
      )}
      {schema.enum ? (
        <select
          {...shared}
          required={required}
          value={
            value === undefined
              ? ""
              : String(schema.enum.findIndex((item) => item === value))
          }
          onChange={(e) =>
            onChange(
              e.target.value === ""
                ? undefined
                : schema.enum![Number(e.target.value)],
            )
          }
        >
          <option value="">Select an option</option>
          {schema.enum.map((item, index) => (
            <option key={index} value={String(index)}>
              {String(item)}
            </option>
          ))}
        </select>
      ) : schema.type === "boolean" ? (
        <label className="checkbox">
          <input
            {...shared}
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => onChange(e.target.checked)}
          />
          <span>{value === true ? "Yes" : "No"}</span>
        </label>
      ) : schema.type === "array" ? (
        <ArrayField
          {...shared}
          required={required}
          value={value}
          onValue={onChange}
        />
      ) : schema.type === "number" || schema.type === "integer" ? (
        <input
          {...shared}
          required={required}
          type="number"
          min={schema.minimum}
          max={schema.maximum}
          step={schema.type === "integer" ? 1 : "any"}
          value={value === undefined ? "" : String(value)}
          onChange={(e) =>
            onChange(e.target.value === "" ? undefined : Number(e.target.value))
          }
        />
      ) : (
        <textarea
          {...shared}
          className={prefix.endsWith(".diff") ? "code" : undefined}
          required={required}
          rows={prefix.endsWith(".diff") ? 6 : 3}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
