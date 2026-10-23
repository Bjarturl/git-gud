from io import BytesIO

from django.contrib import admin
from django.contrib.admin import SimpleListFilter, helpers
from django.db import connection
from django.db.models import Count, Exists, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import path
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from openpyxl import Workbook

from apps.search.models import Match, MatchStatus
from apps.task_queue.backends import enqueue

from .models import Commit, Gist, Repo, Tag, User, UserRelationship, UserStatus


class LanguageFilter(SimpleListFilter):
    title = "programming language"
    parameter_name = "language"

    def lookups(self, request, model_admin):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT unnest(languages) AS language, COUNT(*) AS count
                FROM git_repo
                WHERE languages != '{}'
                GROUP BY unnest(languages)
                ORDER BY count DESC, language
                LIMIT 50
                """
            )
            lang_counts = [(lang, count)
                           for lang, count in cursor.fetchall() if lang]

        return [
            (lang, f"{lang} ({count})" if i < 50 else lang)
            for i, (lang, count) in enumerate(lang_counts)
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(languages__contains=[self.value()])
        return queryset


class RelatedToFilter(SimpleListFilter):
    title = "related to"
    parameter_name = "related_to"
    template = "admin/git_data/user/related_to_filter.html"

    def lookups(self, request, model_admin):
        return [("_", "_")]

    def choices(self, changelist):
        yield {
            "selected": bool(self.value()),
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": "All",
        }

    def queryset(self, request, queryset):
        val = self.value()
        if not val:
            return queryset
        related_user = User.objects.filter(username__iexact=val).first()
        if not related_user:
            return queryset.none()
        return queryset.filter(
            Q(outgoing_relationships__to_user=related_user) |
            Q(incoming_relationships__from_user=related_user)
        ).distinct()


class ScannedFilter(SimpleListFilter):
    title = "scanned"
    parameter_name = "scanned"

    def lookups(self, request, model_admin):
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(scanned_at__isnull=False)
        if self.value() == "no":
            return queryset.filter(scanned_at__isnull=True)
        return queryset


class TagFilter(SimpleListFilter):
    title = "tag"
    parameter_name = "tag"

    def lookups(self, request, model_admin):
        return Tag.objects.values_list("name", "name")

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(tags__name=self.value())
        return queryset


class HasMatchesFilter(SimpleListFilter):
    title = "has matches"
    parameter_name = "has_matches"

    def lookups(self, request, model_admin):
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request, queryset):
        if self.value() not in ("yes", "no"):
            return queryset
        has_match = Exists(
            Match.objects.filter(
                Q(commit__author=OuterRef("pk")) | Q(
                    gist__author=OuterRef("pk"))
            )
        )
        if self.value() == "yes":
            return queryset.filter(has_match)
        return queryset.filter(~has_match)


class RepoInline(admin.TabularInline):
    model = Repo
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def get_queryset(self, request):
        return super().get_queryset(request)

    def has_add_permission(self, request, obj=None):
        return False


class UserRelationshipFromInline(admin.TabularInline):
    model = UserRelationship
    fk_name = "from_user"
    extra = 0
    verbose_name = "Outgoing relationship"
    verbose_name_plural = "Outgoing relationships"

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


class UserRelationshipToInline(admin.TabularInline):
    model = UserRelationship
    fk_name = "to_user"
    extra = 0
    verbose_name = "Incoming relationship"
    verbose_name_plural = "Incoming relationships"

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ["name", "user_count", "created_at"]
    search_fields = ["name"]
    ordering = ["name"]
    actions = ["run_pipeline_action"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_user_count=Count("users"))

    @admin.display(description="Users", ordering="_user_count")
    def user_count(self, obj):
        return obj._user_count

    @admin.action(description="Run pipeline for all confirmed users with selected tags")
    def run_pipeline_action(self, request, queryset):
        users = User.objects.filter(tags__in=queryset, status=UserStatus.CONFIRMED).distinct()
        count = 0
        for user in users:
            enqueue(
                "apps.task_queue.tasks.pipeline_task",
                priority=0,
                name=f"full user scan - {user.username}",
                username=user.username,
            )
            count += 1
        tag_names = ", ".join(queryset.values_list("name", flat=True))
        self.message_user(request, f"Enqueued pipeline for {count} confirmed user(s) with tag(s): {tag_names}.")

    def get_urls(self):
        return [
            path(
                "import-assets/",
                self.admin_site.admin_view(self.import_assets_view),
                name="git_data_tag_import_assets",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["import_assets_url"] = "import-assets/"
        return super().changelist_view(request, extra_context=extra_context)

    def import_assets_view(self, request):
        import json
        import re as re_module
        from apps.search.models import Regex as RegexModel, RegexCategory

        results = None
        error = None

        if request.method == "POST":
            uploaded_files = request.FILES.getlist("asset_file")
            if not uploaded_files:
                error = "No file uploaded."
            else:
                results = []
                errors = []
                for uploaded in uploaded_files:
                    try:
                        raw = json.loads(uploaded.read().decode("utf-8-sig"))
                    except Exception as exc:
                        errors.append(f"{uploaded.name}: Invalid JSON: {exc}")
                        continue
                    try:
                        file_results = self._process_assets(request, raw, RegexModel, RegexCategory)
                        results.extend(file_results)
                    except Exception as exc:
                        errors.append(f"{uploaded.name}: Processing error: {exc}")
                if errors:
                    error = " | ".join(errors)
                if not results:
                    results = []

        return render(request, "admin/git_data/import_assets.html", {
            **self.admin_site.each_context(request),
            "title": "Import Bug Bounty Assets",
            "opts": Tag._meta,
            "results": results,
            "error": error,
        })

    @staticmethod
    def _cidr_to_pattern(cidr: str) -> str | None:
        """Return a regex pattern that matches IPs in the given CIDR block, or None if unparseable."""
        import re as re_module
        import ipaddress
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return None
        # Build a pattern from the common prefix octets.
        octets = str(net.network_address).split(".")
        prefix_len = net.prefixlen
        # Number of full octets covered by the prefix.
        full_octets = prefix_len // 8
        if full_octets == 0:
            return None  # too broad to be useful
        prefix = r"\.".join(re_module.escape(o) for o in octets[:full_octets])
        return prefix + r"\."

    def _process_assets(self, request, raw, RegexModel, RegexCategory):
        import re as re_module

        skip_types = {"Android Play Store", "IOS App Store"}
        by_company = {}

        for asset in raw:
            if asset.get("Coverage") != "In scope":
                continue
            company = asset["Company"]
            raw_asset = asset["Asset"].strip()
            asset_type = asset["Type"]

            if asset_type in skip_types:
                continue

            entry = by_company.setdefault(company, {"wildcards": set(), "urls": set(), "cidrs": {}})

            if asset_type == "Wildcard" and raw_asset.startswith("*."):
                entry["wildcards"].add(raw_asset[2:])
            elif asset_type in ("Wildcard", "URL", "Domain"):
                # Strip path component from URLs like "example.com/path/"
                host = raw_asset.split("/")[0]
                entry["urls"].add(host)
            elif asset_type in ("IP Range", "CIDR", "Other"):
                if "/" in raw_asset:
                    # CIDR block — build an IP-prefix regex.
                    pattern = self._cidr_to_pattern(raw_asset)
                    if pattern:
                        entry["cidrs"][raw_asset] = pattern
                elif re_module.match(r"^\d{1,3}(\.\d{1,3}){3}$", raw_asset):
                    # Single IP address — exact match regex.
                    entry["cidrs"][raw_asset] = re_module.escape(raw_asset)

        summary = []
        ThroughModel = RegexModel.tags.through

        for company, domains in by_company.items():
            tag, tag_created = Tag.objects.get_or_create(name=company)
            wildcards = domains.get("wildcards", set())
            urls = domains.get("urls", set())
            cidrs = domains.get("cidrs", {})

            filtered_urls = {
                u for u in urls
                if not any(u == w or u.endswith("." + w) for w in wildcards)
            }
            all_domains = wildcards | filtered_urls

            # pattern → display name: domains + CIDR-derived prefix patterns.
            pattern_map = {re_module.escape(d): d for d in all_domains}
            for cidr_str, cidr_pattern in cidrs.items():
                pattern_map[cidr_pattern] = cidr_str

            # One query: find which patterns already exist.
            existing_qs = RegexModel.objects.filter(regex_pattern__in=pattern_map)
            existing_by_pattern = {rx.regex_pattern: rx for rx in existing_qs}

            # Bulk-create the missing ones.
            to_create = [
                RegexModel(
                    regex_pattern=p,
                    name=name,
                    category=RegexCategory.URLS_GENERAL,
                    is_active=True,
                )
                for p, name in pattern_map.items()
                if p not in existing_by_pattern
            ]
            RegexModel.objects.bulk_create(to_create, ignore_conflicts=True)

            # Fetch all regex IDs for these patterns (existing + just created).
            all_rx_ids = list(
                RegexModel.objects.filter(regex_pattern__in=pattern_map)
                .values_list("id", flat=True)
            )

            # Bulk-create M2M links, skipping any that already exist.
            already_linked = set(
                ThroughModel.objects.filter(regex_id__in=all_rx_ids, tag=tag)
                .values_list("regex_id", flat=True)
            )
            ThroughModel.objects.bulk_create(
                [ThroughModel(regex_id=rx_id, tag=tag) for rx_id in all_rx_ids if rx_id not in already_linked],
                ignore_conflicts=True,
            )

            summary.append({
                "company": company,
                "tag_created": tag_created,
                "tag": tag,
                "created": len(to_create),
                "existing": len(existing_by_pattern),
                "skipped_urls": len(urls) - len(filtered_urls),
                "cidr_count": len(cidrs),
            })

        return summary



@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["status_actions", "username", "name", "email",
                    "company", "location", "bio", "display_tags", "top_repos"]
    list_display_links = ["username"]
    list_filter = ["account_type", "status", "discovery_method",
                   TagFilter, ScannedFilter, HasMatchesFilter, RelatedToFilter]
    search_fields = ["username", "name", "email", "company", "bio", "location"]
    filter_horizontal = ("tags",)
    actions = ["apply_tag_action", "export_excel"]
    show_full_result_count = False
    inlines = [RepoInline, UserRelationshipFromInline,
               UserRelationshipToInline]
    fieldsets = [
        ("Basic Information", {
            "fields": ["username", "account_type", "discovery_method", "status"],
        }),
        ("Profile", {
            "fields": ["name", "email", "avatar", "bio", "company", "location", "url"],
        }),
        ("Tags", {
            "fields": ["tags", "display_tags"],
        }),
        ("Source Information", {
            "fields": ["source_user_id"],
        }),
        ("Timestamps", {
            "fields": ["created_at", "processed_at", "scanned_at", "source_created_at"],
            "classes": ["collapse"],
        }),
        ("Matches", {
            "fields": ["matches_table", "match_admin_link"],
        }),
    ]

    class Media:
        js = ("admin/js/user_status.js", "admin/js/match_status.js")

    @admin.display(description="Actions")
    def status_actions(self, obj):
        eye_btn = format_html(
            '<a href="{}" target="_blank"'
            ' style="display:flex;align-items:center;justify-content:center;'
            'text-decoration:none;background:#0d6efd;color:white;'
            'width:28px;height:28px;border-radius:4px;">👁</a>',
            obj.url or "#",
        )
        pipeline_btn = format_html(
            '<button type="button" onclick="runPipeline(this, {})"'
            ' style="border:none;background:#17a2b8;color:white;'
            'width:28px;height:28px;border-radius:4px;cursor:pointer;">▶</button>',
            obj.id,
        )
        related_btn = format_html(
            '<a href="/admin/git_data/user/?related_to={}"'
            ' style="display:flex;align-items:center;justify-content:center;'
            'text-decoration:none;background:#6f42c1;color:white;'
            'width:28px;height:28px;border-radius:4px;font-size:13px;">⇆</a>',
            obj.username,
        )
        if obj.status == UserStatus.UNKNOWN:
            return format_html(
                '<div style="display:flex;gap:6px;">'
                '<button type="button" class="status-btn confirm-btn" data-user-id="{0}"'
                ' onclick="confirmUser(this, {0})"'
                ' style="border:none;background:#28a745;color:white;'
                'width:28px;height:28px;border-radius:4px;cursor:pointer;">✓</button>'
                '{1}'
                '<button type="button" class="status-btn hide-btn" data-user-id="{0}"'
                ' onclick="hideUser(this, {0})"'
                ' style="border:none;background:#dc3545;color:white;'
                'width:28px;height:28px;border-radius:4px;cursor:pointer;">✕</button>'
                '{2}{3}'
                '</div>',
                obj.id, eye_btn, pipeline_btn, related_btn,
            )
        elif obj.status == UserStatus.HIDDEN:
            return format_html(
                '<div style="display:flex;gap:6px;">'
                '<button type="button" class="status-btn confirm-btn" data-user-id="{0}"'
                ' onclick="confirmUser(this, {0})"'
                ' style="border:none;background:#28a745;color:white;'
                'width:28px;height:28px;border-radius:4px;cursor:pointer;">✓</button>'
                '{1}{2}{3}'
                '</div>',
                obj.id, eye_btn, pipeline_btn, related_btn,
            )
        else:
            return format_html(
                '<div style="display:flex;gap:6px;">{}{}{}</div>',
                eye_btn, pipeline_btn, related_btn,
            )

    @admin.display(description="Matches")
    def matches_table(self, obj):
        select = (
            "regex",
            "commit", "commit__repo", "commit__author",
            "gist", "gist__author",
            "deleted_in_commit", "deleted_in_commit__repo",
            "deleted_in_gist",
        )
        own = list(
            Match.objects
            .filter(Q(commit__author=obj) | Q(gist__author=obj))
            .exclude(status=MatchStatus.FALSE_POSITIVE)
            .select_related(*select)
            .order_by("regex__name")[:250]
        )
        repo_matches = list(
            Match.objects
            .filter(commit__repo__owner=obj)
            .exclude(commit__author=obj)
            .exclude(status=MatchStatus.FALSE_POSITIVE)
            .select_related(*select)
            .order_by("regex__name")[:250]
        )
        matches = own + repo_matches

        if not matches:
            return "No matches found."

        rows = []
        for m in matches:
            if m.commit:
                repo_cell = format_html(
                    '<a href="{}" target="_blank">{}</a>',
                    m.commit.repo.url or "#",
                    m.commit.repo.full_name,
                )
                source = format_html(
                    '<a href="{}" target="_blank">{}</a>',
                    m.commit.url or "#",
                    m.commit.sha[:7],
                )
                committer = (
                    format_html(
                        '<a href="/admin/git_data/user/{}/change/">{}</a>',
                        m.commit.author_id,
                        m.commit.author.username,
                    )
                    if m.commit.author_id else "—"
                )
            else:
                repo_cell = "—"
                source = format_html(
                    '<a href="{}" target="_blank">gist:{}</a>',
                    m.gist.url or "#",
                    m.gist.gist_id[:8],
                )
                committer = (
                    format_html(
                        '<a href="/admin/git_data/user/{}/change/">{}</a>',
                        m.gist.author_id,
                        m.gist.author.username,
                    )
                    if m.gist.author_id else "—"
                )

            if m.deleted_in_commit_id:
                deleted = format_html(
                    '<a href="{}" target="_blank" style="color:#6c757d">{}</a>',
                    m.deleted_in_commit.url or "#",
                    m.deleted_in_commit.sha[:7],
                )
            elif m.deleted_in_gist_id:
                deleted = format_html(
                    '<a href="{}" target="_blank" style="color:#6c757d">gist:{}</a>',
                    m.deleted_in_gist.url or "#",
                    m.deleted_in_gist.gist_id[:8],
                )
            else:
                deleted = "—"

            actions = format_html(
                "<td style='padding:4px 8px;white-space:nowrap'>"
                "<button type='button' onclick=\"markMatchByUrl(this,'/admin/search/match/mark-match/{0}/interesting/',false)\""
                " style='border:none;background:#ffc107;color:white;width:24px;height:24px;border-radius:3px;cursor:pointer;margin-right:3px;'>★</button>"
                "<button type='button' onclick=\"markMatchByUrl(this,'/admin/search/match/mark-match/{0}/false-positive/',true)\""
                " style='border:none;background:#6c757d;color:white;width:24px;height:24px;border-radius:3px;cursor:pointer;font-size:9px;font-weight:bold;'>FP</button>"
                "</td>",
                m.id,
            )
            rows.append(format_html(
                "<tr>"
                "<td style='padding:4px 8px'>{}</td>"
                "<td style='padding:4px 8px'><code style='word-break:break-all'>{}</code></td>"
                "<td style='padding:4px 8px'>{}</td>"
                "<td style='padding:4px 8px'>{}</td>"
                "<td style='padding:4px 8px'>{}</td>"
                "<td style='padding:4px 8px'>{}</td>"
                "<td style='padding:4px 8px'>{}</td>"
                "{}</tr>",
                m.regex.name or m.regex.regex_pattern[:40],
                m.match[:100],
                repo_cell,
                m.filename or "—",
                committer,
                source,
                deleted,
                actions,
            ))

        header = mark_safe(
            "<thead><tr>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Regex</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Match</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Repo</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>File</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Committer</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Source</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>Deleted in</th>"
            "<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'></th>"
            "</tr></thead>"
        )

        return format_html(
            "<table style='width:100%;border-collapse:collapse;margin-top:8px'>{}<tbody>{}</tbody></table>",
            header,
            mark_safe("".join(rows)),
        )

    @admin.display(description="")
    def match_admin_link(self, obj):
        return format_html(
            '<a href="/admin/search/match/?author_id={}">View all matches in Match admin →</a>',
            obj.pk,
        )

    def get_urls(self):
        return [
            path("hide-user/<int:user_id>/",
                 self.hide_user, name="git_data_user_hide"),
            path("confirm-user/<int:user_id>/",
                 self.confirm_user, name="git_data_user_confirm"),
            path("run-pipeline-ajax/<int:user_id>/",
                 self.admin_site.admin_view(self.run_pipeline_ajax),
                 name="git_data_user_run_pipeline_ajax"),
            path(
                "<int:user_id>/run-pipeline/",
                self.admin_site.admin_view(self.run_pipeline),
                name="git_data_user_run_pipeline",
            ),
            path(
                "apply-tag/",
                self.admin_site.admin_view(self.apply_tag_view),
                name="git_data_user_apply_tag",
            ),
            *super().get_urls(),
        ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        try:
            user = User.objects.get(pk=object_id)
            if user.status == UserStatus.CONFIRMED:
                extra_context["action_buttons"] = [
                    {
                        "url": f"/admin/git_data/user/{object_id}/run-pipeline/",
                        "title": "Run Pipeline",
                        "style": "background-color: #0d6efd; color: white; border-radius: 4px; padding: 8px 16px; text-decoration: none; font-weight: bold; margin-bottom: 12px; display: inline-block;",
                    }
                ]
        except User.DoesNotExist:
            pass
        return super().change_view(request, object_id, form_url, extra_context)

    def run_pipeline_ajax(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            job = enqueue(
                "apps.task_queue.tasks.pipeline_task",
                priority=0,
                name=f"full user scan - {user.username}",
                username=user.username,
            )
            return JsonResponse({
                "success": True,
                "job_id": str(job.id),
                "job_url": f"/admin/task_queue/taskjob/{job.id}/change/",
                "username": user.username,
            })
        except User.DoesNotExist:
            return JsonResponse({"success": False, "error": "User not found"})
        except Exception as exc:
            return JsonResponse({"success": False, "error": str(exc)})

    def run_pipeline(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            job = enqueue(
                "apps.task_queue.tasks.pipeline_task",
                priority=0,
                name=f"full user scan - {user.username}",
                username=user.username,
            )
            messages.success(
                request,
                mark_safe(
                    f'Pipeline started for "{user.username}" — '
                    f'<a href="/admin/task_queue/taskjob/{job.id}/change/">View job {job.id}</a>'
                ),
            )
        except User.DoesNotExist:
            messages.error(request, "User not found")
        except Exception as exc:
            messages.error(request, f"Failed to start pipeline: {exc}")

        return HttpResponseRedirect(f"/admin/git_data/user/{user_id}/change/")

    @admin.action(description="Export selected users to Excel")
    def export_excel(self, request, queryset):
        wb = Workbook()
        ws = wb.active
        ws.title = "Users"

        ws.append([
            "username", "name", "email", "company", "location",
            "bio", "url", "account_type", "status", "discovery_method", "created_at",
        ])

        for obj in queryset.order_by("id").iterator():
            ws.append([
                obj.username,
                obj.name or "",
                obj.email or "",
                obj.company or "",
                obj.location or "",
                obj.bio or "",
                obj.url or "",
                obj.account_type,
                obj.status,
                obj.discovery_method,
                obj.created_at.strftime("%Y-%m-%d %H:%M:%S") if obj.created_at else "",
            ])

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="users.xlsx"'
        return response

    def _update_user_status(self, request, user_id, status):
        updated = User.objects.filter(id=user_id).update(status=status)
        if not updated:
            return JsonResponse({"success": False, "error": "User not found"})

        return JsonResponse({"success": True})

    def hide_user(self, request, user_id):
        return self._update_user_status(request, user_id, UserStatus.HIDDEN)

    def confirm_user(self, request, user_id):
        return self._update_user_status(request, user_id, UserStatus.CONFIRMED)

    def get_list_display(self, request):
        fields = list(super().get_list_display(request))
        if request.GET.get("has_matches") == "yes" and "match_count" not in fields:
            fields.append("match_count")
        return fields

    def get_search_results(self, request, queryset, search_term):
        qs, use_distinct = super().get_search_results(request, queryset, search_term)
        if search_term:
            # Only search repo name (not description — TEXT icontains is a full table scan)
            repo_qs = queryset.filter(repos__name__icontains=search_term, repos__is_fork=False)
            qs = (qs | repo_qs).distinct()
            use_distinct = True
        return qs, use_distinct

    def get_queryset(self, request):
        qs = super().get_queryset(request).prefetch_related(
            "tags",
            Prefetch(
                "repos",
                queryset=Repo.objects.filter(is_fork=False).only("name").order_by("name"),
                to_attr="_prefetched_repos",
            ),
        )
        if request.GET.get("has_matches") == "yes":
            commit_matches = Subquery(
                Match.objects.filter(commit__author=OuterRef("pk"))
                .values("commit__author")
                .annotate(c=Count("id"))
                .values("c")[:1],
                output_field=IntegerField(),
            )
            gist_matches = Subquery(
                Match.objects.filter(gist__author=OuterRef("pk"))
                .values("gist__author")
                .annotate(c=Count("id"))
                .values("c")[:1],
                output_field=IntegerField(),
            )
            qs = qs.annotate(
                _match_count=Coalesce(commit_matches, 0) +
                Coalesce(gist_matches, 0)
            )
        return qs

    @admin.display(description="Matches", ordering="_match_count")
    def match_count(self, obj):
        return obj._match_count

    @admin.display(description="Top Repos")
    def top_repos(self, obj):
        repos = getattr(obj, "_prefetched_repos", None)
        if repos is None:
            repos = list(Repo.objects.filter(owner=obj, is_fork=False).only("name").order_by("name"))
        names = [r.name for r in repos[:5]]
        return ", ".join(names) if names else "—"

    @admin.display(description="Tags")
    def display_tags(self, obj):
        names = [t.name for t in obj.tags.all()]
        return ", ".join(names) if names else "—"

    @admin.action(description="Add tag to selected users")
    def apply_tag_action(self, request, queryset):
        selected = request.POST.getlist(helpers.ACTION_CHECKBOX_NAME)
        return HttpResponseRedirect(
            f"/admin/git_data/user/apply-tag/"
            f"?ids={','.join(selected)}&mode=selected"
        )

    def apply_tag_view(self, request):
        ids_param = request.GET.get("ids", "") or request.POST.get("ids", "")
        mode = request.GET.get("mode", "selected") or request.POST.get("mode", "selected")
        try:
            user_ids = [int(i) for i in ids_param.split(",") if i.strip()]
        except ValueError:
            user_ids = []
        queryset = User.objects.filter(pk__in=user_ids)

        if request.method == "POST" and "apply" in request.POST:
            tag_name = request.POST.get("new_tag", "").strip()
            tag_id = request.POST.get("tag_id", "").strip()
            if tag_name:
                tag, _ = Tag.objects.get_or_create(name=tag_name)
            elif tag_id:
                try:
                    tag = Tag.objects.get(pk=tag_id)
                except Tag.DoesNotExist:
                    messages.error(request, "Tag not found.")
                    tag = None
            else:
                tag = None

            if tag:
                ThroughModel = User.tags.through
                user_ids = list(queryset.values_list("id", flat=True))
                already = set(
                    ThroughModel.objects.filter(tag=tag, user_id__in=user_ids)
                    .values_list("user_id", flat=True)
                )
                ThroughModel.objects.bulk_create(
                    [ThroughModel(user_id=uid, tag=tag) for uid in user_ids if uid not in already],
                    ignore_conflicts=True,
                )
                messages.success(request, f"Added tag '{tag.name}' to {len(user_ids)} user(s).")
            return HttpResponseRedirect("/admin/git_data/user/")

        return render(request, "admin/git_data/apply_tag_action.html", {
            **self.admin_site.each_context(request),
            "title": "Apply Tag",
            "queryset": queryset,
            "user_ids": ids_param,
            "mode": mode,
            "existing_tags": Tag.objects.all(),
            "opts": self.model._meta,
        })

    def get_readonly_fields(self, request, obj=None):
        editable = {"processed_at", "account_type", "status"}
        return [
            field.name for field in self.model._meta.fields
            if field.name not in editable
        ] + ["matches_table", "match_admin_link", "display_tags"]


@admin.register(Repo)
class RepoAdmin(admin.ModelAdmin):
    list_display = ["full_name", "owner", "is_fork",
                    "stars", "created_at"]
    list_filter = ["processed_at", "is_fork", LanguageFilter]
    search_fields = ["name", "full_name", "owner__username", "description"]
    date_hierarchy = "created_at"
    actions = ["export_excel"]

    fieldsets = [
        ("Basic Information", {
            "fields": ["name", "full_name", "owner"],
        }),
        ("Repository Details", {
            "fields": [
                "description",
                "default_branch",
                "url",
                "homepage",
                "is_fork",
                "languages",
            ],
        }),
        ("Statistics", {
            "fields": ["stars", "size"],
        }),
        ("Source Information", {
            "fields": ["source_repo_id", "tags"],
        }),
        ("Timestamps", {
            "fields": ["created_at", "processed_at", "source_created_at"],
            "classes": ["collapse"],
        }),
    ]

    @admin.action(description="Export selected repos to Excel")
    def export_excel(self, request, queryset):
        queryset = queryset.select_related("owner").order_by("id")

        wb = Workbook()
        ws = wb.active
        ws.title = "Repos"

        ws.append([
            "full_name",
            "owner",
            "description",
            "url",
            "homepage",
            "is_fork",
            "languages",
            "stars",
            "size",
            "default_branch",
            "created_at",
        ])

        for obj in queryset.iterator():
            ws.append([
                obj.full_name,
                obj.owner.username if obj.owner_id else "",
                obj.description or "",
                obj.url or "",
                obj.homepage or "",
                obj.is_fork,
                ", ".join(obj.languages) if obj.languages else "",
                obj.stars or 0,
                obj.size or 0,
                obj.default_branch or "",
                obj.created_at.strftime(
                    "%Y-%m-%d %H:%M:%S") if obj.created_at else "",
            ])

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="repos.xlsx"'
        return response

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Commit)
class CommitAdmin(admin.ModelAdmin):
    list_display = ["sha", "repo", "author",
                    "branch_name", "commit_date"]
    list_filter = ["processed_at"]
    search_fields = ["sha", "message", "author__username", "repo__full_name"]
    date_hierarchy = "commit_date"
    list_per_page = 100
    list_select_related = ("repo", "author", "committer")

    fieldsets = [
        ("Commit Information", {
            "fields": ["sha", "repo", "author", "committer"],
        }),
        ("Commit Details", {
            "fields": ["message", "url", "commit_date", "branch_name", "pr_number"],
        }),
        ("Statistics", {
            "fields": ["additions", "deletions"],
        }),
        ("Timestamps", {
            "fields": ["created_at", "processed_at"],
            "classes": ["collapse"],
        }),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("repo", "author", "committer")

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Gist)
class GistAdmin(admin.ModelAdmin):
    list_display = ["gist_id", "revision_id",
                    "author", "description", "is_fork", "source_created_at"]
    list_filter = ["processed_at", "is_fork"]
    search_fields = ["gist_id", "description", "author__username", "filenames"]
    date_hierarchy = "source_created_at"

    fieldsets = [
        ("Gist Information", {
            "fields": ["gist_id", "revision_id", "author"],
        }),
        ("Content", {
            "fields": ["url", "description", "filenames"],
        }),
        ("Timestamps", {
            "fields": ["created_at", "processed_at", "source_created_at"],
            "classes": ["collapse"],
        }),
    ]

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(UserRelationship)
class UserRelationshipAdmin(admin.ModelAdmin):
    list_display = ["from_user", "to_user",
                    "relationship_type", "repo", "created_at"]
    list_filter = ["relationship_type"]
    search_fields = ["from_user__username",
                     "to_user__username", "repo__full_name"]
    date_hierarchy = "created_at"
    list_per_page = 100
    list_max_show_all = 500

    fieldsets = [
        ("Relationship", {
            "fields": ["from_user", "to_user", "relationship_type"],
        }),
        ("Context", {
            "fields": ["repo"],
        }),
        ("Timestamps", {
            "fields": ["created_at"],
            "classes": ["collapse"],
        }),
    ]

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]
