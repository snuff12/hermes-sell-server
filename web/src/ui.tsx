import type { ButtonHTMLAttributes, ReactNode } from "react";

/** Web equivalents of Hermes primitives; Desktop uses @hermes/plugin-sdk. */
export function Button({
  variant = "secondary",
  size = "default",
  busy,
  className = "",
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "text";
  size?: "default" | "sm";
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      {...props}
      className={`button ${className}`}
      data-variant={variant}
      data-size={size}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
    >
      {children}
      {busy && <span aria-hidden="true" className="spinner" />}
    </button>
  );
}
export function Badge({
  children,
  tone = "muted",
}: {
  children: ReactNode;
  tone?: "muted" | "accent" | "success" | "warning";
}) {
  return (
    <span className="badge" data-tone={tone}>
      {children}
    </span>
  );
}
export function Message({
  children,
  error = false,
}: {
  children: ReactNode;
  error?: boolean;
}) {
  return (
    <div
      className="message"
      data-tone={error ? "error" : "info"}
      role={error ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
export function Workspace({
  navigation,
  children,
}: {
  navigation: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="workspace">
      <aside className="sidebar">
        {navigation}
        <div className="sidebar-foot">
          <span className="status-dot" />
          Local development
        </div>
      </aside>
      <main id="main-content" className="workspace-main">
        {children}
      </main>
    </div>
  );
}
export function Toolbar({
  title,
  detail,
  children,
}: {
  title: string;
  detail?: string;
  children?: ReactNode;
}) {
  return (
    <div className="toolbar">
      <div className="breadcrumb">
        <span>Sell</span>
        <span aria-hidden="true">/</span>
        <strong>{title}</strong>
        {detail && <span className="toolbar-detail">{detail}</span>}
      </div>
      <div className="toolbar-actions">{children}</div>
    </div>
  );
}
export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      {children}
    </div>
  );
}
