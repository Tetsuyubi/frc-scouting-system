from .models import CheckInOut, SeasonRank
from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import redirect, render
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone

# --- SHARED HELPER FUNCTION ---
def perform_full_sync(request):
    excel_file = request.FILES.get("excel_file")
    if not excel_file:
        return False, "No file uploaded."

    # Imported here, not at the top: pandas is slow to load and only this import
    # needs it, but the admin module loads on every cold start.
    import pandas as pd

    try:
        # 1. Clear Rankings for fresh start
        SeasonRank.objects.all().delete()

        # 2. Open Excel and find the rankings sheet
        xl = pd.ExcelFile(excel_file)
        print(f"Sheets in file: {xl.sheet_names}")

        df_rank = None
        used_sheet = None

        def try_read_sheet(xl, sheet):
            """Read a sheet, detecting the real header row if header=0 gives empty or unusable data."""
            # Strategy 1: standard read — most Excel files work with this
            try:
                df = pd.read_excel(xl, sheet_name=sheet, header=0)
                # Accept if non-empty AND at least one column name is a real string
                if not df.empty and any(
                    isinstance(c, str) and c.strip() for c in df.columns
                ):
                    return df
            except Exception:
                pass

            # Strategy 2: read raw (no header), then hunt for a row containing "email"
            # to use as the real header row
            try:
                df_raw = pd.read_excel(xl, sheet_name=sheet, header=None)
                if df_raw.empty:
                    return None
                for row_idx in range(min(10, len(df_raw))):
                    row_vals = df_raw.iloc[row_idx].fillna('').astype(str).str.lower().tolist()
                    if any('email' in v for v in row_vals):
                        df = pd.read_excel(xl, sheet_name=sheet, header=row_idx)
                        if not df.empty:
                            return df
            except Exception:
                pass
            return None

        # 'Form Responses X' sheets are log sheets, not summary sheets — skip for rankings
        def is_log_sheet(name):
            return name.strip().lower().startswith('form response')

        # Try 'Report' first (with multiple header strategies), then other non-log sheets
        summary_sheets = (
            ['Report'] if 'Report' in xl.sheet_names else []
        ) + [
            s for s in xl.sheet_names
            if s != 'Report' and not is_log_sheet(s)
        ]

        for sheet in summary_sheets:
            df_temp = try_read_sheet(xl, sheet)
            if df_temp is None:
                print(f"Sheet '{sheet}' is empty or unreadable, skipping.")
                continue
            # Check if any column contains '@' (email values)
            for col in df_temp.columns:
                if df_temp[col].dropna().astype(str).str.contains('@').any():
                    df_rank = df_temp
                    used_sheet = sheet
                    break
            if df_rank is not None:
                break

        # Last resort: compute rankings directly from Form Responses data
        # by summing durations per user (check-in to check-out)
        if df_rank is None:
            print("No summary sheet found. Computing rankings from Form Responses data.")
            log_sheets = [s for s in xl.sheet_names if is_log_sheet(s)]
            user_hours = {}  # email → total decimal hours
            for sheet in log_sheets:
                df_log = try_read_sheet(xl, sheet)
                if df_log is None:
                    continue
                for _, row in df_log.iterrows():
                    try:
                        ts_in = pd.to_datetime(row.iloc[0], errors='coerce')
                        email = str(row.iloc[1]).strip().lower() if not pd.isna(row.iloc[1]) else ''
                        if pd.isna(ts_in) or '@' not in email:
                            continue
                        # Look for a check-out time in remaining columns
                        ts_out = None
                        for idx in range(2, len(row)):
                            val = pd.to_datetime(row.iloc[idx], errors='coerce')
                            if not pd.isna(val) and val > ts_in:
                                ts_out = val
                                break
                        if ts_out:
                            hrs = (ts_out - ts_in).total_seconds() / 3600
                            user_hours[email] = user_hours.get(email, 0) + hrs
                    except Exception:
                        continue

            rank_count = 0
            for email_raw, total_hrs in user_hours.items():
                user = User.objects.filter(email__iexact=email_raw).first()
                if not user:
                    username_suggestion = email_raw.split('@')[0]
                    if User.objects.filter(username=username_suggestion).exists():
                        username_suggestion = email_raw
                    user = User.objects.create(email=email_raw, username=username_suggestion)
                SeasonRank.objects.update_or_create(user=user, defaults={'total_hours': round(total_hrs, 2)})
                rank_count += 1

            log_count = 0
            msg = (
                f"Sync complete (computed from logs). Rankings synced: {rank_count}. "
                f"Sheets available: {xl.sheet_names}"
            )
            return True, msg

        report_columns = list(df_rank.columns)
        print(f"--- STARTING RANKING SYNC from sheet '{used_sheet}' --- Columns: {report_columns}")

        # 3. Auto-detect email and hours columns by scanning column names then values
        HOURS_NAME_HINTS = {
            'total hours', 'hours', 'total', 'hrs', 'total hrs',
            'hrs total', 'hours total', 'hrs. total', 'hour total',
            'season hours', 'season total',
        }

        email_col_idx = None
        hours_col_idx = None

        # Find email column: first one whose values contain '@'
        for i, col in enumerate(df_rank.columns):
            if df_rank[col].dropna().astype(str).str.contains('@').any():
                email_col_idx = i
                break

        # Find hours column: prefer a column whose name hints at hours
        for i, col in enumerate(df_rank.columns):
            if i == email_col_idx:
                continue
            if str(col).strip().lower() in HOURS_NAME_HINTS:
                hours_col_idx = i
                break

        # Fallback: among numeric non-email columns, pick the one with the
        # highest median — avoids picking grade/year columns (9-12) over hours (50-200)
        if hours_col_idx is None:
            best_median = -1
            for i, col in enumerate(df_rank.columns):
                if i == email_col_idx:
                    continue
                if pd.api.types.is_numeric_dtype(df_rank[col]):
                    med = df_rank[col].dropna().median()
                    if med > best_median:
                        best_median = med
                        hours_col_idx = i

        detected_email_col = df_rank.columns[email_col_idx] if email_col_idx is not None else "NOT FOUND"
        detected_hours_col = df_rank.columns[hours_col_idx] if hours_col_idx is not None else "NOT FOUND"
        print(f"Using: email col='{detected_email_col}' ({email_col_idx}), hours col='{detected_hours_col}' ({hours_col_idx})")

        rank_count = 0
        skip_count = 0
        for _, row in df_rank.iterrows():
            email_raw_val = row.iloc[email_col_idx]
            if pd.isna(email_raw_val):
                skip_count += 1
                continue

            email_raw = str(email_raw_val).strip().lower()
            if '@' not in email_raw:
                skip_count += 1
                continue

            hours_raw = row.iloc[hours_col_idx] if hours_col_idx is not None else None

            user = User.objects.filter(email__iexact=email_raw).first()
            if not user:
                username_suggestion = email_raw.split('@')[0]
                if User.objects.filter(username=username_suggestion).exists():
                    username_suggestion = email_raw
                user = User.objects.create(email=email_raw, username=username_suggestion)
                print(f"DEBUG: Created new user for {email_raw}")

            try:
                h_val = float(hours_raw) if hours_raw is not None and not pd.isna(hours_raw) else 0.0
            except (ValueError, TypeError):
                h_val = 0.0

            SeasonRank.objects.update_or_create(user=user, defaults={'total_hours': h_val})
            rank_count += 1
            print(f"DEBUG: Synced {user.email} → {h_val} hrs")

        # 4. Sync Logs (optional — only runs if the sheet exists in this file)
        log_count = 0
        if 'Form Responses 6' in xl.sheet_names:
            print("--- STARTING LOGS SYNC ---")
            df_logs = pd.read_excel(xl, sheet_name='Form Responses 6')
            for _, row in df_logs.iterrows():
                if pd.isna(row.iloc[0]) or pd.isna(row.iloc[1]): continue
                c_time = pd.to_datetime(row.iloc[0], errors='coerce')
                if pd.isna(c_time): continue

                email = str(row.iloc[1]).strip().lower()
                user = User.objects.filter(email__iexact=email).first()
                if user:
                    CheckInOut.objects.update_or_create(
                        user=user,
                        check_in_time=c_time,
                        defaults={
                            'reason': 'Excel Import',
                            'logic': str(row.iloc[2])[:45] if not pd.isna(row.iloc[2]) else ""
                        }
                    )
                    log_count += 1

        msg = (
            f"Sync complete. Rankings synced: {rank_count}, skipped: {skip_count}. "
            f"Log entries: {log_count}. "
            f"Sheet used: '{used_sheet}' (available: {xl.sheet_names}). "
            f"Email col: '{detected_email_col}', Hours col: '{detected_hours_col}'. "
            f"Columns: {report_columns}"
        )
        return True, msg
    except Exception as e:
        print(f"SYNC ERROR: {e}")
        import traceback; traceback.print_exc()
        return False, f"Error: {e}"

@admin.register(SeasonRank)
class SeasonRankAdmin(admin.ModelAdmin):
    list_display = ('user', 'total_hours', 'last_updated')
    search_fields = ('user__username', 'user__email')
    change_list_template = "admin/hours_app/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('import-excel/', self.admin_site.admin_view(self.import_view), name='rankings_import_excel'),
        ]
        return custom_urls + urls

    def import_view(self, request):
        if request.method == "POST":
            success, msg = perform_full_sync(request)
            if success: messages.success(request, msg)
            else: messages.error(request, msg)
            return redirect("..")
        return render(request, 'admin/hours_app/import_excel.html', {'title': 'Rankings Sync'})

@admin.register(CheckInOut)
class CheckInOutAdmin(admin.ModelAdmin):
    list_display = ('student_name', 'military_time', 'logic', 'daily_hours', 'reason')
    search_fields = ('user__username', 'user__email')
    list_filter = ('check_in_time', 'logic')
    change_list_template = "admin/hours_app/checkinout_change_list.html"
    actions = ['check_out_selected']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('import-excel/', self.admin_site.admin_view(self.import_view), name='logs_import_excel'),
            path('checkout-all/', self.admin_site.admin_view(self.checkout_all_view), name='checkinout_checkout_all'),
        ]
        return custom_urls + urls

    def import_view(self, request):
        if request.method == "POST":
            success, msg = perform_full_sync(request)
            if success: messages.success(request, msg)
            else: messages.error(request, msg)
            return redirect("..")
        return render(request, 'admin/hours_app/import_excel.html', {'title': 'Logs Sync'})

    def checkout_all_view(self, request):
        if request.method == "POST":
            now = timezone.now()
            count = CheckInOut.objects.filter(check_out_time__isnull=True).update(check_out_time=now)
            messages.success(request, f"Checked out {count} active session(s).")
        return redirect("..")

    @admin.action(description="Mark selected as checked out (now)")
    def check_out_selected(self, request, queryset):
        now = timezone.now()
        count = queryset.filter(check_out_time__isnull=True).update(check_out_time=now)
        self.message_user(request, f"Checked out {count} session(s).")

    def student_name(self, obj): return obj.user.username
    def military_time(self, obj): return obj.check_in_time.strftime('%H:%M') if obj.check_in_time else "--:--"
    def daily_hours(self, obj):
        if obj.check_in_time and obj.check_out_time:
            diff = obj.check_out_time - obj.check_in_time
            return round(diff.total_seconds() / 3600, 2)
        return "0.0"


class CheckInOutInline(admin.TabularInline):
    model = CheckInOut
    extra = 0
    can_delete = False
    readonly_fields = ('check_in_time', 'check_out_time', 'logic', 'accomplishments', 'reason')
    fields = ('check_in_time', 'check_out_time', 'logic', 'accomplishments', 'reason')
    ordering = ('-check_in_time',)
    verbose_name = "Sign-in/out log"
    verbose_name_plural = "Sign-in/out logs"

    def has_add_permission(self, request, obj=None):
        return False


class CustomUserAdmin(UserAdmin):
    inlines = [CheckInOutInline]


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)