import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";

// Wizard-модалка для добавления нового Telegram-аккаунта через auth-flow:
//   step "form"  — телефон + имя + прокси → POST /auth/start
//   step "code"  — код из SMS/TG → POST /auth/code (может попросить 2FA)
//   step "2fa"   — облачный пароль → POST /auth/2fa
// На любом этапе отмена → DELETE /auth/{session_id} (если есть) + закрытие.

type Step = "form" | "code" | "2fa";

type Props = {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;  // triggers accounts refetch in parent
};

export function NewAccountDialog({ open, onClose, onCreated }: Props) {
  const toast = useToast();
  const [step, setStep] = useState<Step>("form");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);

  // form fields
  const [phone, setPhone] = useState("");
  const [name, setName] = useState("");
  const [proxyPrimary, setProxyPrimary] = useState("");
  const [proxyFallback, setProxyFallback] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");

  function reset() {
    setStep("form");
    setBusy(false);
    setSessionId(null);
    setPhone(""); setName(""); setProxyPrimary(""); setProxyFallback("");
    setCode(""); setPassword("");
  }

  async function handleClose() {
    if (sessionId) {
      // best-effort cancel — отлетевшая сессия в auth_service не критична,
      // TTL её всё равно закроет.
      api.authCancel(sessionId).catch(() => {});
    }
    reset();
    onClose();
  }

  async function submitForm() {
    setBusy(true);
    try {
      const res = await api.authStart({
        phone: phone.trim(),
        name: name.trim(),
        proxy_primary: proxyPrimary.trim(),
        proxy_fallback: proxyFallback.trim(),
      });
      setSessionId(res.session_id);
      setStep("code");
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Auth start: ${d.title}`, d.detail);
    } finally {
      setBusy(false);
    }
  }

  async function submitCode() {
    if (!sessionId) return;
    setBusy(true);
    try {
      const res = await api.authCode(sessionId, code.trim());
      if (res.status === "2fa_required") {
        setStep("2fa");
      } else {
        toast.success("Аккаунт добавлен", res.account_id ? `account_id=${res.account_id}` : undefined);
        reset();
        onCreated();
        onClose();
      }
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Auth code: ${d.title}`, d.detail);
    } finally {
      setBusy(false);
    }
  }

  async function submit2fa() {
    if (!sessionId) return;
    setBusy(true);
    try {
      const res = await api.auth2fa(sessionId, password);
      toast.success("Аккаунт добавлен", res.account_id ? `account_id=${res.account_id}` : undefined);
      reset();
      onCreated();
      onClose();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Auth 2FA: ${d.title}`, d.detail);
    } finally {
      setBusy(false);
    }
  }

  const formValid =
    phone.trim().length >= 5 && name.trim().length > 0 &&
    proxyPrimary.trim().length > 0 && proxyFallback.trim().length > 0;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            Новый аккаунт
            <span className="ml-2 mono text-[10px] text-muted-foreground">
              {step === "form" ? "1/3 · данные" : step === "code" ? "2/3 · код" : "3/3 · 2FA"}
            </span>
          </DialogTitle>
        </DialogHeader>

        {step === "form" && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-phone">Телефон</Label>
              <Input id="acc-phone" placeholder="+7999..." value={phone} onChange={(e) => setPhone(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-name">Имя (отображаемое)</Label>
              <Input id="acc-name" placeholder="Валерия / Тест и т.п." value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-proxy1">Proxy (primary)</Label>
              <Input id="acc-proxy1" placeholder="socks5://user:pass@host:port" value={proxyPrimary} onChange={(e) => setProxyPrimary(e.target.value)} className="mono" />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-proxy2">Proxy (fallback)</Label>
              <Input id="acc-proxy2" placeholder="socks5://..." value={proxyFallback} onChange={(e) => setProxyFallback(e.target.value)} className="mono" />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" size="sm" onClick={handleClose} disabled={busy}>Отмена</Button>
              <Button size="sm" onClick={submitForm} disabled={!formValid || busy}>
                {busy ? "…" : "Отправить код"}
              </Button>
            </div>
          </div>
        )}

        {step === "code" && (
          <div className="flex flex-col gap-3">
            <div className="text-xs text-muted-foreground">
              Код отправлен в Telegram / SMS на {phone}. Введи его ниже.
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-code">Код</Label>
              <Input
                id="acc-code"
                inputMode="numeric"
                autoFocus
                placeholder="12345"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className="mono"
              />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" size="sm" onClick={handleClose} disabled={busy}>Отмена</Button>
              <Button size="sm" onClick={submitCode} disabled={!code.trim() || busy}>
                {busy ? "…" : "Подтвердить"}
              </Button>
            </div>
          </div>
        )}

        {step === "2fa" && (
          <div className="flex flex-col gap-3">
            <div className="text-xs text-muted-foreground">
              На аккаунте включён облачный пароль. Введи его чтобы завершить вход.
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="acc-2fa">Облачный пароль (2FA)</Label>
              <Input
                id="acc-2fa"
                type="password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" size="sm" onClick={handleClose} disabled={busy}>Отмена</Button>
              <Button size="sm" onClick={submit2fa} disabled={!password || busy}>
                {busy ? "…" : "Войти"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
