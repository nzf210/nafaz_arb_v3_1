"""Telegram HTML message formatters and inline keyboard layouts for nafaz_arb v3.1."""

import html
import time
from typing import Any, Dict, List, Optional

MENU_UTAMA_TEXT = "📱 Menu Utama"
TEXT_SIMULASI = "SIMULASI"
TEXT_LIVE = "LIVE"
TEXT_USDT = "USDT"
SEPARATOR = "----------------------------------------\n"


def default_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ Status", "callback_data": "status"},
                {"text": "💼 Posisi Aktif", "callback_data": "positions"},
            ],
            [
                {"text": "💰 Cek Balance", "callback_data": "balance"},
                {"text": "📜 Riwayat", "callback_data": "history"},
            ],
            [
                {"text": "📋 Peluang Terbaru", "callback_data": "latest"},
                {"text": "📊 Statistik Metrik", "callback_data": "metrics"},
            ],
            [
                {"text": "⚙️ Pengaturan", "callback_data": "settings"},
                {"text": "🔔 Notifikasi", "callback_data": "notif"},
                {"text": "🛡️ Risk Status", "callback_data": "risk"},
            ],
            [
                {"text": "🔄 Refresh Menu", "callback_data": "menu"},
                {"text": "ℹ️ Bantuan", "callback_data": "help"},
            ],
        ]
    }


def settings_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔔 Filter Notifikasi", "callback_data": "notif"},
                {"text": "🛑 Toggle Kill-Switch", "callback_data": "toggle_killswitch"},
            ],
            [
                {"text": "⚡ Status", "callback_data": "status"},
                {"text": MENU_UTAMA_TEXT, "callback_data": "menu"},
            ],
        ]
    }


def notif_keyboard(filters: Dict[str, bool]) -> Dict[str, Any]:
    def _lbl(name: str, key: str) -> str:
        return f"{name}: {'✅ ON' if filters.get(key, True) else '❌ OFF'}"

    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Hanya Trade", "callback_data": "notif:trades_only"},
                {"text": "🔔 Semua ON", "callback_data": "notif:all_on"},
            ],
            [
                {"text": _lbl("Peluang", "opps"), "callback_data": "notif:toggle:opps"},
                {"text": _lbl("Scan Reject", "rejections"), "callback_data": "notif:toggle:rejections"},
            ],
            [
                {"text": _lbl("Order Fill", "fills"), "callback_data": "notif:toggle:fills"},
                {"text": _lbl("Posisi Close", "closes"), "callback_data": "notif:toggle:closes"},
            ],
            [
                {"text": _lbl("Error Alert", "errors"), "callback_data": "notif:toggle:errors"},
                {"text": "🔕 Mute Semua", "callback_data": "notif:all_off"},
            ],
            [
                {"text": "⚙️ Pengaturan", "callback_data": "settings"},
                {"text": MENU_UTAMA_TEXT, "callback_data": "menu"},
            ],
        ]
    }


def positions_keyboard(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Dict[str, str]]] = []
    for pos in positions:
        asset = str(pos.get("base_asset", "")).upper()
        if asset:
            rows.append([{"text": f"🚨 Tutup Paksa {asset}", "callback_data": f"force_close:{asset}"}])
    rows.append([
        {"text": "📜 Riwayat", "callback_data": "history"},
        {"text": "🔄 Refresh Posisi", "callback_data": "positions"},
    ])
    rows.append([{"text": MENU_UTAMA_TEXT, "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def confirm_force_close_keyboard(base_asset: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Ya, Eksekusi Sekarang", "callback_data": f"confirm_fc:{base_asset}"},
                {"text": "❌ Batalkan", "callback_data": "cancel_fc"},
            ],
            [{"text": MENU_UTAMA_TEXT, "callback_data": "menu"}],
        ]
    }


def format_menu() -> str:
    return (
        "🤖 <b>Menu Interaktif nafaz_arb v3.1</b>\n\n"
        "Gunakan tombol navigasi di bawah atau ketik command langsung:\n"
        "• <code>/status</code> — Kondisi engine, bursa, & kill-switch\n"
        "• <code>/positions</code> — Posisi aktif Spot-Perp & tombol force close\n"
        "• <code>/balance</code> — Saldo live seluruh CEX\n"
        "• <code>/history</code> — 10 eksekusi transaksi terakhir\n"
        "• <code>/latest</code> — 10 sinyal peluang arbitrase terakhir\n"
        "• <code>/settings</code> — Konfigurasi profil & limit modal\n"
        "• <code>/risk</code> — Status margin ratio & ADL rank\n"
        "• <code>/notif</code> — Filter notifikasi Telegram\n"
        "• <code>/killswitch on|off</code> — Emergency trading halt"
    )


def format_help() -> str:
    return (
        "ℹ️ <b>Panduan Perintah nafaz_arb v3.1</b>\n\n"
        "<b>Perintah Monitoring:</b>\n"
        "• <code>/menu</code> — Buka tombol menu interaktif\n"
        "• <code>/status</code> — Ringkasan status sistem & bursa\n"
        "• <code>/positions</code> — Pantau posisi terbuka & PnL\n"
        "• <code>/balance</code> — Cek saldo live exchange\n"
        "• <code>/history</code> — Riwayat eksekusi selesai\n"
        "• <code>/latest</code> — Peluang dari scan terakhir\n"
        "• <code>/pair &lt;ASET&gt;</code> — Cek peluang koin tertentu (cth: /pair BTC)\n"
        "• <code>/metrics</code> — Statistik rejeksi & evaluasi\n\n"
        "<b>Perintah Operasional & Risiko:</b>\n"
        "• <code>/risk</code> — Peringatan margin ratio & ADL\n"
        "• <code>/reconcile [retry]</code> — Rekonsiliasi posisi 1:1\n"
        "• <code>/settings</code> — Lihat parameter aktif\n"
        "• <code>/set &lt;param&gt; &lt;nilai&gt;</code> — Ubah parameter (cth: /set canary 50)\n"
        "• <code>/notif [trades|all|pause]</code> — Filter alert push\n"
        "• <code>/mute [durasi]</code> / <code>/unmute</code> — Bisukan alert sementara\n"
        "• <code>/killswitch on|off</code> — Emergency stop/start"
    )


def format_status(info: Dict[str, Any]) -> str:
    dry = info.get("dry_run", True)
    mode_tag = TEXT_SIMULASI if dry else TEXT_LIVE
    ks = "🛑 AKTIF (HALTED)" if info.get("kill_switch") else "🟢 NONAKTIF (NORMAL)"
    pos_count = info.get("open_positions_count", 0)
    cexs = info.get("cex_health", {})
    cex_lines = "\n".join(
        f"• <b>{c.upper()}</b>: <code>{status}</code>" for c, status in cexs.items()
    ) or "• <i>Belum ada heartbeat bursa</i>"

    return (
        "⚡ <b>Status nafaz_arb v3.1</b>\n\n"
        f"• <b>Mode Trading:</b> <code>{mode_tag}</code>\n"
        f"• <b>Kill-Switch:</b> <b>{ks}</b>\n"
        f"• <b>Posisi Terbuka:</b> <code>{pos_count}</code>\n"
        f"• <b>Canary Cap:</b> <code>${info.get('canary_cap', 0):.2f} {TEXT_USDT}</code>\n"
        f"• <b>Postgres DB:</b> <code>{'TERHUBUNG' if info.get('db_ok') else 'OFFLINE'}</code>\n"
        f"• <b>Alert Filter:</b> <code>{info.get('notif_summary', 'Normal')}</code>\n\n"
        f"🏦 <b>Kesehatan CEX Adapter:</b>\n{cex_lines}"
    )


def _format_pos_item(i: int, pos: Dict[str, Any]) -> str:
    asset = html.escape(str(pos.get("base_asset", ""))).upper()
    qty = float(pos.get("qty", 0.0))
    pid = html.escape(str(pos.get("profile_id", "")))
    leg_a = pos.get("leg_a", {})
    leg_b = pos.get("leg_b", {})
    ex_a = html.escape(str(leg_a.get("exchange", ""))).upper()
    ex_b = html.escape(str(leg_b.get("exchange", ""))).upper()
    sym_a = html.escape(str(leg_a.get("symbol") or leg_a.get("raw_symbol", "")))
    sym_b = html.escape(str(leg_b.get("symbol") or leg_b.get("raw_symbol", "")))
    pr_a = float(leg_a.get("price") or leg_a.get("vwap_price") or 0.0)
    pr_b = float(leg_b.get("price") or leg_b.get("vwap_price") or 0.0)
    act_a = html.escape(str(leg_a.get("action", "BUY"))).upper()
    act_b = html.escape(str(leg_b.get("action", "SELL"))).upper()

    return (
        f"<b>[{i}] {asset}</b> ({pid})\n"
        f"• Kuantitas: <code>{qty:.6f} {asset}</code>\n"
        f"• Kaki A: <b>{ex_a}</b> <code>{act_a}</code> {sym_a} @ {pr_a:.4f}\n"
        f"• Kaki B: <b>{ex_b}</b> <code>{act_b}</code> {sym_b} @ {pr_b:.4f}\n"
    )


def format_positions(positions: List[Dict[str, Any]]) -> str:
    if not positions:
        return (
            "💼 <b>Status Posisi Executor</b>\n\n"
            "<i>Saat ini tidak ada posisi arbitrase yang terbuka (idle).</i>"
        )
    blocks = [_format_pos_item(i + 1, p) for i, p in enumerate(positions)]
    return (
        f"💼 <b>Posisi Aktif Executor ({len(positions)} Posisi)</b>\n\n"
        + SEPARATOR
        + SEPARATOR.join(blocks)
        + "\n<i>Tekan tombol di bawah untuk Tutup Paksa posisi tertentu.</i>"
    )


def format_balance(exchanges_data: Dict[str, Dict[str, Any]], dry_run: bool) -> str:
    mode_tag = TEXT_SIMULASI if dry_run else TEXT_LIVE
    lines = [f"💰 <b>BALANCE BURSA ({mode_tag})</b>\n"]
    if dry_run or not exchanges_data:
        lines.append("⚠️ <i>Mode Simulasi: saldo riil bursa tidak dimuat.</i>\n")
    for ex_name, bal in exchanges_data.items():
        lines.append(f"🏦 <b>{ex_name.upper()}:</b>")
        usdt_free = float(bal.get("free", {}).get(TEXT_USDT, 0.0))
        usdt_total = float(bal.get("total", {}).get(TEXT_USDT, 0.0))
        lines.append(f"• <b>{TEXT_USDT}:</b> Free <code>${usdt_free:.2f}</code> | Total <code>${usdt_total:.2f}</code>")
        for asset, total in bal.get("total", {}).items():
            if asset != TEXT_USDT and float(total or 0) > 0.0001:
                free = float(bal.get("free", {}).get(asset, 0.0))
                lines.append(f"• <b>{asset}:</b> Free <code>{free:.4f}</code> | Total <code>{float(total):.4f}</code>")
        lines.append("")
    return "\n".join(lines).strip()


def format_history(executions: List[Dict[str, Any]]) -> str:
    if not executions:
        return "📜 <b>Riwayat Transaksi</b>\n\n<i>Belum ada eksekusi tercatat di database.</i>"
    lines = ["📜 <b>10 Riwayat Transaksi Terakhir</b>\n"]
    for i, ex in enumerate(executions, 1):
        asset = html.escape(str(ex.get("base_asset", ""))).upper()
        st = html.escape(str(ex.get("status", "")))
        qty = float(ex.get("qty", 0.0))
        notional = float(ex.get("effective_notional_usdt", 0.0))
        dry = TEXT_SIMULASI if ex.get("dry_run") else TEXT_LIVE
        t = str(ex.get("created_at", ""))[:19]
        lines.append(
            f"<b>{i}. {asset}</b> — <code>{st}</code> [{dry}]\n"
            f"• Qty: <code>{qty:.4f}</code> | Notional: <code>${notional:.2f}</code> | {t}\n"
        )
    return "\n".join(lines).strip()


def format_latest_signals(signals: List[Dict[str, Any]]) -> str:
    if not signals:
        return "📋 <b>Peluang Sinyal Terbaru</b>\n\n<i>Belum ada audit sinyal di database.</i>"
    lines = ["📋 <b>10 Sinyal Arbitrase Terakhir</b>\n"]
    for i, s in enumerate(signals, 1):
        asset = html.escape(str(s.get("base_asset", ""))).upper()
        route = html.escape(str(s.get("route_type", "")))
        st = html.escape(str(s.get("status", "")))
        notional = float(s.get("effective_notional_usdt") or s.get("target_notional_usdt", 0.0))
        reason = html.escape(str(s.get("reason") or "-"))
        ex = html.escape(str(s.get("exchange") or "")).strip()
        if ex:
            reason = f"{reason} ({ex})"
        lines.append(
            f"<b>{i}. {asset}</b> ({route}) — <code>{st}</code>\n"
            f"• Target: <code>${notional:.2f}</code> | Alasan: <i>{reason}</i>\n"
        )
    return "\n".join(lines).strip()


REJECTION_REASON_LABELS: Dict[str, str] = {
    "negative_gross": "Funding Gross <= 0",
    "high_breakeven": "Breakeven Terlalu Lama",
    "low_net_yield": "Net Yield di Bawah Minimum",
    "low_volume": "Volume 24j Kurang",
    "insufficient_depth": "Kedalaman Orderbook Kurang",
    "high_basis_spread": "Spread Basis Terlalu Lebar",
    "collateral_ratio_low": "Rasio Kolateral Rendah",
    "stale_book": "Orderbook Kedaluwarsa",
    "stale_funding": "Data Funding Kedaluwarsa",
    "stale_collateral": "Data Kolateral Kedaluwarsa",
}


def format_rejection_metrics(stats: Dict[str, int]) -> str:
    lines = ["📊 <b>Metrik Rejeksi & Evaluasi Scanner</b>\n"]
    valid_items = sorted(
        [(str(k), int(v)) for k, v in (stats or {}).items() if int(v) > 0],
        key=lambda kv: kv[1],
        reverse=True,
    )
    total = sum(v for _, v in valid_items)
    if not valid_items or total <= 0:
        lines.append("<i>Belum ada statistik rejeksi tercatat di Redis.</i>")
        return "\n".join(lines)
    lines.append(f"• <b>Total Evaluasi Ditolak:</b> <code>{total:,}</code> kali\n")
    for reason, count in valid_items:
        label = html.escape(REJECTION_REASON_LABELS.get(reason, reason))
        code = html.escape(reason)
        pct = (count / total) * 100.0
        lines.append(f"• <b>{label}</b> (<code>{code}</code>): <b>{count:,}</b> kali ({pct:.1f}%)")
    return "\n".join(lines)


def format_risk_status(risk_data: Dict[str, Any]) -> str:
    margins = risk_data.get("margins", {})
    adls = risk_data.get("adls", [])
    drifts = risk_data.get("drifts", [])

    lines = ["🛡️ <b>Status Manajemen Risiko</b>\n"]
    lines.append("• <b>Margin Usage CEX:</b>")
    if margins:
        for ex, r in margins.items():
            lines.append(f"  - <b>{ex.upper()}</b>: <code>{float(r):.2%}</code>")
    else:
        lines.append("  - <i>Semua aman / belum ada posisi leverage</i>")

    lines.append("\n• <b>Indikator ADL:</b>")
    if adls:
        for a in adls:
            lines.append(f"  - ⚠️ {a.get('base_asset')}: Rank <code>{a.get('adl_rank')}/5</code>")
    else:
        lines.append("  - 🟢 <i>Tidak ada risiko de-leveraging terdeteksi</i>")

    lines.append("\n• <b>Position Drift 1:1:</b>")
    if drifts:
        for d in drifts:
            lines.append(f"  - ⚠️ {d.get('base_asset')}: Drift <code>{d.get('leg_drift')}</code>")
    else:
        lines.append("  - 🟢 <i>Posisi kedua kaki seimbang sempurna</i>")
    return "\n".join(lines).strip()


def format_settings(profiles: Dict[str, Any], global_dry: bool, canary_cap: float) -> str:
    lines = [
        "⚙️ <b>Pengaturan Runtime nafaz_arb v3.1</b>\n",
        f"• <b>Global Default Dry-Run:</b> <code>{global_dry}</code>",
        f"• <b>Default Canary Cap:</b> <code>${canary_cap:.2f} {TEXT_USDT}</code>\n",
        "<b>Profil Sinyal Aktif:</b>",
    ]
    for pid, p in profiles.items():
        en = "✅ ON" if p.get("enabled", True) else "❌ OFF"
        dry = TEXT_SIMULASI if p.get("dry_run", global_dry) else TEXT_LIVE
        cap = p.get("canary_max_notional_usdt") or canary_cap
        lines.append(f"• <code>{pid}</code>: {en} | [{dry}] | Cap: ${float(cap):.2f}")
    lines.append(
        "\n📝 <b>Ubah Parameter via Command:</b>\n"
        "• <code>/set canary &lt;usd&gt;</code> (cth: /set canary 50)\n"
        "• <code>/set dryrun &lt;true|false&gt;</code>\n"
        "• <code>/set profile &lt;id&gt; enable|disable</code>"
    )
    return "\n".join(lines).strip()


def format_notif_settings(filters: Dict[str, bool], muted_until_str: str = "") -> str:
    st = lambda k: "🟢 <b>AKTIF</b>" if filters.get(k, True) else "🔴 <i>NONAKTIF</i>"
    mute_line = f"• <b>Status Mute:</b> 🔕 Dibisukan s/d {muted_until_str}\n" if muted_until_str else ""
    return (
        "🔔 <b>Pengaturan Filter Notifikasi</b>\n\n"
        + mute_line
        + f"• <b>Peluang Baru (Opportunities):</b> {st('opps')}\n"
        f"• <b>Alasan Rejeksi Scan:</b> {st('rejections')}\n"
        f"• <b>Order Fill / Open Posisi:</b> {st('fills')}\n"
        f"• <b>Posisi Closed / Selesai:</b> {st('closes')}\n"
        f"• <b>Error & Risk Alert:</b> {st('errors')}\n\n"
        "<i>Perintah Cepat:</i>\n"
        "• <code>/notif trades</code> — Hanya notif entry/close posisi\n"
        "• <code>/notif all</code> — Nyalakan semua notifikasi\n"
        "• <code>/notif pause</code> — Matikan semua notifikasi\n"
        "• <code>/notif toggle &lt;opps|rejections|fills|closes|errors&gt;</code>"
    )


def format_position_opened_alert(signal: Dict[str, Any], result: Dict[str, Any]) -> str:
    dry = result.get("dry_run", True)
    mode = TEXT_SIMULASI if dry else TEXT_LIVE
    asset = html.escape(str(signal.get("base_asset", ""))).upper()
    qty = float(result.get("qty") or result.get("qty_filled_a") or 0.0)
    notional = float(result.get("effective_notional_usdt") or signal.get("target_notional_usdt", 0.0))
    legs = signal.get("legs", {})
    la = legs.get("leg_a", {})
    lb = legs.get("leg_b", {})
    return (
        f"🟢 <b>[{mode}] POSISI ARBITRASE TERBUKA!</b>\n\n"
        f"• Pasangan: <b>{asset}</b>\n"
        f"• Kuantitas: <code>{qty:.6f} {asset}</code> (~${notional:.2f} {TEXT_USDT})\n"
        f"• Kaki A: <b>{html.escape(str(la.get('exchange', ''))).upper()}</b> <code>{html.escape(str(la.get('action', ''))).upper()}</code> @ {float(la.get('vwap_price', 0)):.4f}\n"
        f"• Kaki B: <b>{html.escape(str(lb.get('exchange', ''))).upper()}</b> <code>{html.escape(str(lb.get('action', ''))).upper()}</code> @ {float(lb.get('vwap_price', 0)):.4f}\n"
        f"• Profile: <code>{html.escape(str(signal.get('profile_id', '')))}</code>\n\n"
        "<i>Gunakan /positions untuk memantau posisi terbuka.</i>"
    )


def format_position_closed_alert(unwind_result: Dict[str, Any], base_asset: str) -> str:
    status = unwind_result.get("status", "UNWOUND")
    asset = html.escape(base_asset).upper()
    return (
        f"🎉 <b>POSISI SELESAI DITUTUP!</b>\n\n"
        f"• Aset: <b>{asset}</b>\n"
        f"• Status Unwind: <code>{status}</code>\n"
        f"• Flat Verifikasi: <code>{unwind_result.get('verified_flat', True)}</code>\n\n"
        "<i>Ketik /history untuk melihat riwayat eksekusi.</i>"
    )
