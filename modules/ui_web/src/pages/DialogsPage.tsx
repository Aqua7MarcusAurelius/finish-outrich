import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { X, Bot, Zap, Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AccountCard } from "@/components/dialogs/AccountCard";
import { NewAccountCard } from "@/components/dialogs/NewAccountCard";
import { NewAccountDialog } from "@/components/dialogs/NewAccountDialog";
import { NewDialogButton } from "@/components/dialogs/NewDialogButton";
import { NewDialogDialog } from "@/components/dialogs/NewDialogDialog";
import { DeleteDialogConfirm } from "@/components/dialogs/DeleteDialogConfirm";
import { WipeDialogsConfirm } from "@/components/dialogs/WipeDialogsConfirm";
import { AutoRunnerSettingsDialog } from "@/components/dialogs/AutoRunnerSettingsDialog";
import { DialogEventsWidget } from "@/components/dialogs/DialogEventsWidget";
import { DialogListItem } from "@/components/dialogs/DialogListItem";
import { LeadInfoPanel } from "@/components/dialogs/LeadInfoPanel";
import { AllWorkersDropdown } from "@/components/dialogs/AllWorkersDropdown";
import { MessageBubble } from "@/components/dialogs/MessageBubble";
import { ErrorBox } from "@/components/common/ErrorBox";
import { useAccounts, useDialogProfile, useDialogs, useMessages } from "@/hooks/useAccounts";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";

export function DialogsPage() {
  const navigate = useNavigate();
  const params = useParams<{ accountId?: string; dialogId?: string }>();
  const toast = useToast();

  const accountsQ = useAccounts();
  const accounts = accountsQ.data ?? [];

  const accountId = params.accountId ? Number(params.accountId) : accounts[0]?.id ?? null;
  const dialogId = params.dialogId ? Number(params.dialogId) : null;

  const dialogsQ = useDialogs(accountId);
  const dialogs = dialogsQ.data?.items ?? [];

  const messagesQ = useMessages(dialogId);
  const dialogQ = useDialogProfile(dialogId);

  const [search, setSearch] = useState("");
  const [newAccOpen, setNewAccOpen] = useState(false);
  const [newDlgOpen, setNewDlgOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [autochatBusy, setAutochatBusy] = useState(false);
  // Wipe-all-dialogs модалка: какой аккаунт сейчас в фокусе кнопки + busy
  const [wipeTargetId, setWipeTargetId] = useState<number | null>(null);
  const [wipeBusy, setWipeBusy] = useState(false);
  // Авторежим: какой аккаунт открыли в модалке настроек.
  const [arSettingsAccount, setArSettingsAccount] = useState<number | null>(null);

  // Polling статуса автодиалога каждые 5с — чтобы кнопка вживую отражала
  // ситуацию (например, LLM сама поставила <finishdialog/> и сессия
  // погасла). enabled только когда выбран диалог.
  const autochatQ = useQuery({
    queryKey: ["dialog-autochat", dialogId],
    queryFn: () => api.getDialogAutochat(dialogId!),
    enabled: dialogId != null,
    refetchInterval: 5_000,
  });

  async function toggleAutochat() {
    if (dialogId == null || autochatQ.data == null) return;
    setAutochatBusy(true);
    try {
      if (autochatQ.data.active) {
        await api.disableDialogAutochat(dialogId);
        toast.success("Автодиалог выключен");
      } else {
        await api.enableDialogAutochat(dialogId);
        toast.success("Автодиалог включён");
      }
      autochatQ.refetch();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Автодиалог: ${d.title}`, d.detail);
    } finally {
      setAutochatBusy(false);
    }
  }

  const [regenBusy, setRegenBusy] = useState(false);
  async function regenerateReply() {
    if (dialogId == null) return;
    setRegenBusy(true);
    try {
      await api.regenerateDialogAutochat(dialogId);
      toast.success(
        "Генерация запущена",
        "ответ сейчас уйдёт через обычный sender",
      );
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Регенерация: ${d.title}`, d.detail);
    } finally {
      setRegenBusy(false);
    }
  }

  async function confirmDelete() {
    if (dialogId == null) return;
    setDeleteBusy(true);
    try {
      await api.deleteDialog(dialogId);
      toast.success("Диалог удалён");
      setDeleteOpen(false);
      // Сбрасываем выбранный диалог и обновляем список диалогов аккаунта
      dialogsQ.refetch();
      navigate(`/dialogs/${accountId ?? ""}`);
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Удаление: ${d.title}`, d.detail);
    } finally {
      setDeleteBusy(false);
    }
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return dialogs;
    return dialogs.filter((d) =>
      [d.title, d.username, d.phone].filter(Boolean).some((s) => s!.toLowerCase().includes(q)),
    );
  }, [dialogs, search]);

  // Auto-select first dialog when an account opens
  useEffect(() => {
    if (accountId && !dialogId && filtered.length > 0) {
      navigate(`/dialogs/${accountId}/${filtered[0].id}`, { replace: true });
    }
  }, [accountId, dialogId, filtered, navigate]);

  return (
    <div className="flex h-full flex-col">
      {/* ── Accounts row ─────────────────────────────────────────────
         Карточки воркеров в горизонтальном скролле. Справа — кнопка
         «всё списком» (выпадающий dropdown) для быстрой навигации
         когда воркеров много. */}
      <div className="flex items-stretch border-b border-border">
        <div className="min-w-0 flex-1">
          <ScrollArea className="w-full" orientation="horizontal">
            <div className="flex gap-2 p-2 pb-3">
              {accountsQ.isError && (
                <ErrorBox title="Не удалось загрузить аккаунты" detail={String(accountsQ.error)} />
              )}
              {accountsQ.isLoading && <div className="text-xs text-muted-foreground">загрузка…</div>}
              {accounts.map((a) => (
                <AccountCard
                  key={a.id}
                  account={a}
                  selected={a.id === accountId}
                  onSelect={() => navigate(`/dialogs/${a.id}`)}
                  onStart={() => api.startWorker(a.id)
                    .then(() => { toast.success(`Воркер ${a.name || `#${a.id}`} запущен`); accountsQ.refetch(); })
                    .catch((e) => { const d = describeApiError(e); toast.error(`Start: ${d.title}`, d.detail); })}
                  onStop={() => api.stopWorker(a.id)
                    .then(() => { toast.success(`Воркер ${a.name || `#${a.id}`} остановлен`); accountsQ.refetch(); })
                    .catch((e) => { const d = describeApiError(e); toast.error(`Stop: ${d.title}`, d.detail); })}
                  onEditPrompt={() => navigate(`/workers/${a.id}/prompt`)}
                  onWipeDialogs={() => setWipeTargetId(a.id)}
                  onAutoRunnerSettings={() => setArSettingsAccount(a.id)}
                />
              ))}
              <NewAccountCard onClick={() => setNewAccOpen(true)} />
            </div>
          </ScrollArea>
        </div>
        <div className="flex shrink-0 items-center px-2">
          <AllWorkersDropdown
            accounts={accounts}
            currentAccountId={accountId}
            onPick={(id) => navigate(`/dialogs/${id}`)}
          />
        </div>
      </div>

      <NewAccountDialog
        open={newAccOpen}
        onClose={() => setNewAccOpen(false)}
        onCreated={() => accountsQ.refetch()}
      />

      <NewDialogDialog
        open={newDlgOpen}
        accountId={accountId}
        onClose={() => setNewDlgOpen(false)}
        onCreated={() => {
          // Новый диалог-row в history.dialogs появится после первого
          // message.saved (~1-3 сек). Рефетчим с запасом, два тика.
          accountsQ.refetch();
          setTimeout(() => dialogsQ.refetch(), 2500);
        }}
      />

      <DeleteDialogConfirm
        open={deleteOpen}
        dialogTitle={
          dialogQ.data
            ? `${dialogQ.data.first_name || ""} ${dialogQ.data.last_name || ""}`.trim() ||
              (dialogQ.data.username ? `@${dialogQ.data.username}` : `dialog #${dialogQ.data.id}`)
            : ""
        }
        busy={deleteBusy}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteOpen(false)}
      />

      <AutoRunnerSettingsDialog
        open={arSettingsAccount !== null}
        accountId={arSettingsAccount}
        accountLabel={(() => {
          const a = accounts.find((x) => x.id === arSettingsAccount);
          return a ? (a.name || `account_${a.id}`) : "";
        })()}
        onClose={() => setArSettingsAccount(null)}
      />

      {/* Wipe ALL dialogs of an account — двойная защита: открытие модалки
          + ввод строки CONFIRM. Бэк перепроверяет тот же литерал. */}
      <WipeDialogsConfirm
        open={wipeTargetId !== null}
        accountLabel={(() => {
          const a = accounts.find((x) => x.id === wipeTargetId);
          if (!a) return "";
          return a.name ? `${a.name} (${a.phone})` : a.phone || `account #${a.id}`;
        })()}
        dialogsCount={accounts.find((x) => x.id === wipeTargetId)?.dialogs_count ?? 0}
        busy={wipeBusy}
        onConfirm={async () => {
          if (wipeTargetId == null) return;
          setWipeBusy(true);
          try {
            const r = await api.wipeAccountDialogs(wipeTargetId);
            const partGone = r.deleted_local_only > 0
              ? ` Из них ${r.deleted_local_only} — только локально (в Telegram их уже не было).`
              : "";
            if (r.stopped && r.reason === "flood_wait") {
              toast.error(
                `Wipe прервался (FloodWait)`,
                `Удалено: ${r.deleted}/${r.processed_total}. Повтори через ${r.retry_after_seconds ?? "?"} сек.${partGone}`,
              );
            } else if (r.failed.length > 0) {
              toast.error(
                `Wipe закончился с ошибками`,
                `Удалено: ${r.deleted}/${r.processed_total}, ошибок: ${r.failed.length}.${partGone}`,
              );
            } else {
              toast.success(
                `Wipe завершён`,
                `Удалено диалогов: ${r.deleted}.${partGone}`,
              );
            }
            // Список диалогов и счётчик в карточке аккаунта меняются — рефетчим.
            accountsQ.refetch();
            dialogsQ.refetch();
            // Если открыт диалог — он мог быть удалён, сбросим выделение.
            navigate(`/dialogs/${wipeTargetId}`);
            setWipeTargetId(null);
          } catch (e) {
            const d = describeApiError(e);
            toast.error(`Wipe: ${d.title}`, d.detail);
          } finally {
            setWipeBusy(false);
          }
        }}
        onCancel={() => setWipeTargetId(null)}
      />

      {/* ── Body: dialogs + messages ─────────────────────────────── */}
      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[320px] shrink-0 flex-col border-r border-border">
          <div className="flex h-12 shrink-0 items-center border-b border-border px-2">
            <Input placeholder="Поиск по диалогам…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <ScrollArea className="flex-1">
            {dialogsQ.isError && <div className="p-2"><ErrorBox title="API недоступен" detail={String(dialogsQ.error)} /></div>}
            {dialogsQ.isLoading && <div className="p-3 text-xs text-muted-foreground">загрузка…</div>}
            {!dialogsQ.isLoading && filtered.length === 0 && (
              <div className="p-3 text-xs text-muted-foreground">диалогов пока нет</div>
            )}
            {filtered.map((d) => (
              <DialogListItem
                key={d.id}
                dialog={d}
                selected={d.id === dialogId}
                onSelect={() => navigate(`/dialogs/${accountId}/${d.id}`)}
                onStatusChange={() => dialogsQ.refetch()}
              />
            ))}
          </ScrollArea>
          {/* sticky footer: вне ScrollArea, остаётся видимым при прокрутке */}
          <NewDialogButton
            onClick={() => setNewDlgOpen(true)}
            disabled={accountId == null}
            hint="Выбери аккаунт сверху"
          />
        </aside>

        {/* Правая область делится на две колонки:
             1. чат — w-[780px], отделён от правого поля бордером;
             2. место под будущий виджет (профиль / related events) — flex-1. */}
        <section className="flex min-w-0 flex-1 flex-row">
          <div className="flex w-[780px] shrink-0 flex-col border-r border-border">
            {/* Высота h-12 совпадает с шапкой "Поиск по диалогам" слева —
               нижняя линия бордера идёт сквозной без ступеньки. */}
            <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-2">
              <div className="flex items-center gap-3">
                {dialogQ.data ? (
                  <>
                    <div className="text-sm font-semibold">
                      {dialogQ.data.first_name || ""} {dialogQ.data.last_name || ""}
                    </div>
                    <div className="mono text-xs text-muted-foreground">
                      {dialogQ.data.username ? `@${dialogQ.data.username}` : "—"} · {dialogQ.data.phone ?? "—"} · dialog #{dialogQ.data.id}
                    </div>
                  </>
                ) : (
                  <div className="text-xs text-muted-foreground">выберите диалог</div>
                )}
              </div>
              {dialogQ.data && (
                <div className="flex items-center gap-1">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={toggleAutochat}
                    disabled={autochatBusy || autochatQ.isLoading}
                    title={
                      autochatQ.data?.active
                        ? "Автодиалог запущен — клик чтобы выключить"
                        : "Автодиалог выключен — клик чтобы включить (просто начнёт отвечать на новые входящие)"
                    }
                    className="h-7 gap-1.5 px-2 text-[11px]"
                  >
                    <span
                      className={
                        autochatQ.data?.active
                          ? "h-1.5 w-1.5 rounded-full bg-status-success"
                          : "h-1.5 w-1.5 rounded-full bg-muted-foreground"
                      }
                    />
                    <Bot className="h-3 w-3" />
                    Авто: {autochatQ.data?.active ? "ВКЛ" : "ВЫКЛ"}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={regenerateReply}
                    disabled={regenBusy || !autochatQ.data?.active}
                    title={
                      autochatQ.data?.active
                        ? "Сгенерить ответ сейчас (пропустить reply_timer)"
                        : "Сначала включи автодиалог"
                    }
                    className="h-7 gap-1.5 px-2 text-[11px]"
                  >
                    {regenBusy
                      ? <Loader2 className="h-3 w-3 animate-spin" />
                      : <Zap className="h-3 w-3" />}
                    Сгенерить
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setDeleteOpen(true)}
                    title="Удалить диалог (необратимо)"
                    className="h-7 w-7 text-destructive hover:bg-destructive/10 hover:text-destructive"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              )}
            </header>
            {/* flex-col-reverse: backend отдаёт сообщения в DESC (новые первыми),
               в DOM они идут как пришли, но flex-col-reverse кладёт первый
               элемент визуально в низ. Результат — старые сверху, новые снизу,
               как в любом мессенджере. Скролл по умолчанию в самом низу потому
               что scrollTop=0 в reverse-контейнере = крайнее нижнее положение. */}
            <div className="flex-1 overflow-y-auto">
              <div className="flex flex-col-reverse gap-2 p-3">
                {messagesQ.isError && <ErrorBox title="Сообщения не загрузились" detail={String(messagesQ.error)} />}
                {messagesQ.isLoading && <div className="text-xs text-muted-foreground">загрузка…</div>}
                {(messagesQ.data?.items ?? []).map((m) => (
                  <MessageBubble key={m.id} m={m} />
                ))}
              </div>
            </div>
          </div>

          {/* Правая колонка — инфа о лиде (10.10) + поток событий. */}
          <div className="flex min-w-0 flex-1 flex-col">
            {dialogQ.data && <LeadInfoPanel dialog={dialogQ.data} />}
            <div className="min-h-0 flex-1">
              <DialogEventsWidget dialogId={dialogId} />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
