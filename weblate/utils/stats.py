# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import timedelta
from itertools import chain
from time import monotonic
from types import GeneratorType

import sentry_sdk
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Length
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from weblate.checks.models import CHECKS
from weblate.lang.models import Language
from weblate.trans.mixins import BaseURLMixin
from weblate.trans.util import translation_percent
from weblate.utils.db import conditional_sum
from weblate.utils.random import get_random_identifier
from weblate.utils.site import get_site_url
from weblate.utils.state import (
    STATE_APPROVED,
    STATE_EMPTY,
    STATE_FUZZY,
    STATE_READONLY,
    STATE_TRANSLATED,
)

BASICS = {
    "all",
    "fuzzy",
    "todo",
    "readonly",
    "nottranslated",
    "translated",
    "approved",
    "allchecks",
    "translated_checks",
    "dismissed_checks",
    "suggestions",
    "nosuggestions",
    "comments",
    "approved_suggestions",
    "unlabeled",
    "unapproved",
}
BASIC_KEYS = frozenset(
    (
        *(f"{x}_words" for x in BASICS),
        *(f"{x}_chars" for x in BASICS),
        *BASICS,
        "languages",
        "last_changed",
        "last_author",
        "recent_changes",
        "monthly_changes",
        "total_changes",
    )
)
SOURCE_KEYS = frozenset(
    (
        *BASIC_KEYS,
        "source_strings",
        "source_words",
        "source_chars",
    )
)


def aggregate(stats, item, stats_obj):
    if item == "stats_timestamp":
        stats[item] = max(stats[item], getattr(stats_obj, item))
    elif item == "last_changed":
        last = stats["last_changed"]
        if stats_obj.last_changed and (not last or last < stats_obj.last_changed):
            stats["last_changed"] = stats_obj.last_changed
            stats["last_author"] = stats_obj.last_author
    elif item != "last_author":
        # The last_author is calculated with last_changed
        stats[item] += getattr(stats_obj, item)


def zero_stats(keys):
    stats = {item: 0 for item in keys}
    stats["last_changed"] = None
    stats["last_author"] = None
    stats["stats_timestamp"] = 0
    return stats


def prefetch_stats(queryset):
    """Fetch stats from cache for a queryset."""
    # Force evaluating queryset/iterator, we need all objects
    objects = list(queryset)

    # This function can either accept queryset, in which case it is
    # returned with prefetched stats, or iterator, in which case new list
    # is returned.
    # This is needed to allow using such querysets further and to support
    # processing iterator when it is more effective.
    result = objects if isinstance(queryset, GeneratorType) else queryset

    # Bail out in case the query is empty
    if not objects:
        return result

    # Use stats prefetch
    objects[0].stats.prefetch_many([i.stats for i in objects])

    return result


class BaseStats:
    """Caching statistics calculator."""

    basic_keys = BASIC_KEYS
    is_ghost = False

    def __init__(self, obj):
        self._object = obj
        self._data = None
        self._pending_save = False
        self.last_change_cache = None

    @property
    def pk(self):
        return self._object.pk

    def get_absolute_url(self):
        return self._object.get_absolute_url()

    def get_translate_url(self):
        return self._object.get_translate_url()

    @property
    def obj(self):
        return self._object

    @property
    def stats(self):
        return self

    @property
    def is_loaded(self):
        return self._data is not None

    def set_data(self, data):
        self._data = data

    def get_data(self):
        """
        Return a copy of data including percents.

        Used in stats endpoints.
        """
        percents = [
            "translated_percent",
            "approved_percent",
            "fuzzy_percent",
            "readonly_percent",
            "allchecks_percent",
            "translated_checks_percent",
            "translated_words_percent",
            "approved_words_percent",
            "fuzzy_words_percent",
            "readonly_words_percent",
            "allchecks_words_percent",
            "translated_checks_words_percent",
        ]
        data = {percent: self.calculate_percent(percent) for percent in percents}
        data.update(self._data)
        return data

    @staticmethod
    def prefetch_many(stats):
        lookup = {i.cache_key: i for i in stats if not i.is_loaded}
        if not lookup:
            return
        data = cache.get_many(lookup.keys())
        for item, value in data.items():
            lookup[item].set_data(value)
        for item in set(lookup.keys()) - set(data.keys()):
            lookup[item].set_data({})

    @cached_property
    def has_review(self):
        return True

    @cached_property
    def cache_key(self):
        return f"stats-{self._object.cache_key}"

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(f"Invalid stats for {self}: {name}")
        if self._data is None:
            self._data = self.load()
        if name.endswith("_percent"):
            return self.calculate_percent(name)
        if name == "stats_timestamp":
            # TODO: Drop in Weblate 5.3
            # Migration path for legacy stat data
            return self._data.get(name, 0)
        if name not in self._data:
            was_pending = self._pending_save
            self._pending_save = True
            self.calculate_by_name(name)
            if name not in self._data:
                raise AttributeError(f"Unsupported stats for {self}: {name}")
            if not was_pending:
                self.save()
                self._pending_save = False
        return self._data[name]

    def calculate_by_name(self, name: str):
        if name in self.basic_keys:
            self.calculate_basic()
            self.save()

    def load(self):
        return cache.get(self.cache_key, {})

    def save(self, update_parents: bool = True):
        """Save stats to cache."""
        cache.set(self.cache_key, self._data, 30 * 86400)

    def get_update_objects(self):
        yield GlobalStats()

    def update_parents(self, extra_objects: list[BaseStats] | None = None):
        # Get unique list of stats to update.
        # This preserves ordering so that closest ones are updated first.
        stat_objects = {stat.cache_key: stat for stat in self.get_update_objects()}
        if extra_objects:
            for stat in extra_objects:
                stat_objects[stat.cache_key] = stat

        # Update stats
        for stat in prefetch_stats(stat_objects.values()):
            if self.stats_timestamp and self.stats_timestamp <= stat.stats_timestamp:
                continue
            self._object.log_debug("updating stats for %s", stat._object)
            stat.update_stats()

    def clear(self):
        """Clear local cache."""
        self._data = {}

    def store(self, key, value):
        if self._data is None:
            self._data = self.load()
        if value is None and not key.startswith("last_"):
            self._data[key] = 0
        else:
            self._data[key] = value

    def update_stats(self, update_parents: bool = True):
        self.clear()
        if settings.STATS_LAZY:
            self.save(update_parents=update_parents)
        else:
            self.calculate_basic()
            self.save(update_parents=update_parents)

    def calculate_basic(self):
        with sentry_sdk.start_span(
            op="stats", description=f"CALCULATE {self.cache_key}"
        ):
            self._calculate_basic()
            # Store timestamp
            self.store("stats_timestamp", monotonic())

    def _calculate_basic(self):
        raise NotImplementedError

    def calculate_percent(self, item: str) -> float:
        """Calculate percent value for given item."""
        base = item[:-8]

        if base.endswith("_words"):
            total = self.all_words
        elif base.endswith("_chars"):
            total = self.all_chars
        else:
            total = self.all

        if self.has_review:
            completed = {"approved", "approved_words", "approved_chars"}
        else:
            completed = {"translated", "translated_words", "translated_chars"}

        return translation_percent(
            getattr(self, base), total, zero_complete=(base in completed)
        )

    @property
    def waiting_review_percent(self):
        return self.translated_percent - self.approved_percent - self.readonly_percent

    @property
    def waiting_review(self):
        return self.translated - self.approved - self.readonly

    @property
    def waiting_review_words_percent(self):
        return (
            self.translated_words_percent
            - self.approved_words_percent
            - self.readonly_words_percent
        )

    @property
    def waiting_review_words(self):
        return self.translated_words - self.approved_words - self.readonly_words

    @property
    def waiting_review_chars_percent(self):
        return (
            self.translated_chars_percent
            - self.approved_chars_percent
            - self.readonly_chars_percent
        )

    @property
    def waiting_review_chars(self):
        return self.translated_chars - self.approved_chars - self.readonly_chars


class DummyTranslationStats(BaseStats):
    """
    Dummy stats to report 0 in all cases.

    Used when given language does not exist in a component.
    """

    def __init__(self, obj):
        super().__init__(obj)
        self.language = obj

    @property
    def pk(self):
        return f"l-{self.language.pk}"

    def cache_key(self):
        return None

    def save(self, update_parents: bool = True):
        return

    def load(self):
        return {}

    def _calculate_basic(self):
        self._data = zero_stats(self.basic_keys)


class TranslationStats(BaseStats):
    """Per translation stats."""

    def save(self, update_parents: bool = True):
        from weblate.utils.tasks import update_translation_stats_parents

        super().save()

        if update_parents:
            if settings.CELERY_TASK_ALWAYS_EAGER:
                transaction.on_commit(self.update_parents)
            else:
                pk = self._object.pk
                transaction.on_commit(
                    lambda: update_translation_stats_parents.delay(pk)
                )

    def get_update_objects(self):
        yield self._object.language.stats
        yield from self._object.language.stats.get_update_objects()

        yield self._object.component.stats
        yield from self._object.component.stats.get_update_objects()

        project_language = ProjectLanguage(
            project=self._object.component.project, language=self._object.language
        )
        yield project_language.stats
        yield from project_language.stats.get_update_objects()

        yield from super().get_update_objects()

    @property
    def language(self):
        return self._object.language

    @cached_property
    def has_review(self):
        return self._object.enable_review

    def _calculate_basic(self):
        base = self._object.unit_set.annotate(
            active_checks_count=Count("check", filter=Q(check__dismissed=False)),
            dismissed_checks_count=Count("check", filter=Q(check__dismissed=True)),
            suggestion_count=Count("suggestion"),
            comment_count=Count("comment", filter=Q(comment__resolved=False)),
        )
        stats = base.aggregate(
            all=Count("id"),
            all_words=Sum("num_words"),
            all_chars=Sum(Length("source")),
            fuzzy=conditional_sum(1, state=STATE_FUZZY),
            fuzzy_words=conditional_sum("num_words", state=STATE_FUZZY),
            fuzzy_chars=conditional_sum(Length("source"), state=STATE_FUZZY),
            readonly=conditional_sum(1, state=STATE_READONLY),
            readonly_words=conditional_sum("num_words", state=STATE_READONLY),
            readonly_chars=conditional_sum(Length("source"), state=STATE_READONLY),
            translated=conditional_sum(1, state__gte=STATE_TRANSLATED),
            translated_words=conditional_sum("num_words", state__gte=STATE_TRANSLATED),
            translated_chars=conditional_sum(
                Length("source"), state__gte=STATE_TRANSLATED
            ),
            todo=conditional_sum(1, state__lt=STATE_TRANSLATED),
            todo_words=conditional_sum("num_words", state__lt=STATE_TRANSLATED),
            todo_chars=conditional_sum(Length("source"), state__lt=STATE_TRANSLATED),
            nottranslated=conditional_sum(1, state=STATE_EMPTY),
            nottranslated_words=conditional_sum("num_words", state=STATE_EMPTY),
            nottranslated_chars=conditional_sum(Length("source"), state=STATE_EMPTY),
            # Review workflow
            approved=conditional_sum(1, state=STATE_APPROVED),
            approved_words=conditional_sum("num_words", state=STATE_APPROVED),
            approved_chars=conditional_sum(Length("source"), state=STATE_APPROVED),
            unapproved=conditional_sum(1, state=STATE_TRANSLATED),
            unapproved_words=conditional_sum("num_words", state=STATE_TRANSLATED),
            unapproved_chars=conditional_sum(Length("source"), state=STATE_TRANSLATED),
            # Labels
            unlabeled=conditional_sum(1, source_unit__labels__isnull=True),
            unlabeled_words=conditional_sum(
                "num_words", source_unit__labels__isnull=True
            ),
            unlabeled_chars=conditional_sum(
                Length("source"), source_unit__labels__isnull=True
            ),
            # Checks
            allchecks=conditional_sum(1, active_checks_count__gt=0),
            allchecks_words=conditional_sum("num_words", active_checks_count__gt=0),
            allchecks_chars=conditional_sum(
                Length("source"), active_checks_count__gt=0
            ),
            translated_checks=conditional_sum(
                1, state=STATE_TRANSLATED, active_checks_count__gt=0
            ),
            translated_checks_words=conditional_sum(
                "num_words", state=STATE_TRANSLATED, active_checks_count__gt=0
            ),
            translated_checks_chars=conditional_sum(
                Length("source"), state=STATE_TRANSLATED, active_checks_count__gt=0
            ),
            dismissed_checks=conditional_sum(1, dismissed_checks_count__gt=0),
            dismissed_checks_words=conditional_sum(
                "num_words", dismissed_checks_count__gt=0
            ),
            dismissed_checks_chars=conditional_sum(
                Length("source"), dismissed_checks_count__gt=0
            ),
            # Suggestions
            suggestions=conditional_sum(1, suggestion_count__gt=0),
            suggestions_words=conditional_sum("num_words", suggestion_count__gt=0),
            suggestions_chars=conditional_sum(Length("source"), suggestion_count__gt=0),
            nosuggestions=conditional_sum(
                1, state__lt=STATE_TRANSLATED, suggestion_count=0
            ),
            nosuggestions_words=conditional_sum(
                "num_words", state__lt=STATE_TRANSLATED, suggestion_count=0
            ),
            nosuggestions_chars=conditional_sum(
                Length("source"), state__lt=STATE_TRANSLATED, suggestion_count=0
            ),
            approved_suggestions=conditional_sum(
                1, state__gte=STATE_APPROVED, suggestion_count__gt=0
            ),
            approved_suggestions_words=conditional_sum(
                "num_words", state__gte=STATE_APPROVED, suggestion_count__gt=0
            ),
            approved_suggestions_chars=conditional_sum(
                Length("source"), state__gte=STATE_APPROVED, suggestion_count__gt=0
            ),
            # Comments
            comments=conditional_sum(1, comment_count__gt=0),
            comments_words=conditional_sum("num_words", comment_count__gt=0),
            comments_chars=conditional_sum(Length("source"), comment_count__gt=0),
        )
        for key, value in stats.items():
            self.store(key, value)

        # There is single language here, but it is aggregated at higher levels
        self.store("languages", 1)

        # Last change timestamp
        self.fetch_last_change()

        self.count_changes()

    def get_last_change_obj(self):
        from weblate.trans.models import Change

        # This is set in Change.save
        if self.last_change_cache is not None:
            return self.last_change_cache

        cache_key = Change.get_last_change_cache_key(self._object.pk)
        change_pk = cache.get(cache_key)
        if change_pk == 0:
            # No change
            return None
        if change_pk is not None:
            try:
                return Change.objects.get(pk=change_pk)
            except Change.DoesNotExist:
                pass
        try:
            last_change = self._object.change_set.content().order()[0]
        except IndexError:
            Change.store_last_change(self._object, None)
            return None
        last_change.update_cache_last_change()
        return last_change

    def fetch_last_change(self):
        last_change = self.get_last_change_obj()

        if last_change is None:
            self.store("last_changed", None)
            self.store("last_author", None)
        else:
            self.store("last_changed", last_change.timestamp)
            self.store("last_author", last_change.author_id)

    def count_changes(self):
        if self.last_changed:
            monthly = timezone.now() - timedelta(days=30)
            recently = self.last_changed - timedelta(hours=6)
            changes = self._object.change_set.content().aggregate(
                total=Count("id"),
                recent=conditional_sum(timestamp__gt=recently),
                monthly=conditional_sum(timestamp__gt=monthly),
            )
            self.store("recent_changes", changes["recent"])
            self.store("monthly_changes", changes["monthly"])
            self.store("total_changes", changes["total"])
        else:
            self.store("recent_changes", 0)
            self.store("monthly_changes", 0)
            self.store("total_changes", 0)

    def calculate_by_name(self, name: str):
        super().calculate_by_name(name)
        if name.startswith("check:"):
            self.calculate_checks()
        elif name.startswith("label:"):
            self.calculate_labels()

    def calculate_checks(self):
        """Prefetch check stats."""
        allchecks = {check.url_id for check in CHECKS.values()}
        stats = (
            self._object.unit_set.filter(check__dismissed=False)
            .values("check__name")
            .annotate(
                strings=Count("pk"), words=Sum("num_words"), chars=Sum(Length("source"))
            )
        )
        for stat in stats:
            check = stat["check__name"]
            # Filtering here is way more effective than in SQL
            if check is None:
                continue
            check = f"check:{check}"
            self.store(check, stat["strings"])
            self.store(check + "_words", stat["words"])
            self.store(check + "_chars", stat["chars"])
            allchecks.discard(check)
        for check in allchecks:
            self.store(check, 0)
            self.store(check + "_words", 0)
            self.store(check + "_chars", 0)
        self.save()

    def calculate_labels(self):
        """Prefetch check stats."""
        from weblate.trans.models.label import TRANSLATION_LABELS

        alllabels = set(
            self._object.component.project.label_set.values_list("name", flat=True)
        )
        stats = self._object.unit_set.values("source_unit__labels__name").annotate(
            strings=Count("pk"), words=Sum("num_words"), chars=Sum(Length("source"))
        )
        translation_stats = (
            self._object.unit_set.filter(
                labels__name__in=TRANSLATION_LABELS,
            )
            .values("labels__name")
            .annotate(
                strings=Count("pk"), words=Sum("num_words"), chars=Sum(Length("source"))
            )
        )

        for stat in chain(stats, translation_stats):
            label_name = stat.get("source_unit__labels__name", stat.get("labels__name"))
            # Filtering here is way more effective than in SQL
            if label_name is None:
                continue
            label = f"label:{label_name}"
            self.store(label, stat["strings"])
            self.store(label + "_words", stat["words"])
            self.store(label + "_chars", stat["chars"])
            alllabels.discard(label_name)
        for label_name in alllabels:
            label = f"label:{label_name}"
            self.store(label, 0)
            self.store(label + "_words", 0)
            self.store(label + "_chars", 0)
        self.save()


class AggregatingStats(BaseStats):
    basic_keys = SOURCE_KEYS

    @cached_property
    def object_set(self):
        raise NotImplementedError

    @cached_property
    def category_set(self):
        return []

    def calculate_source(self, stats_obj, stats):
        """
        Sums strings/words/chars in all translations.

        This pretty much matches what aggegate does.
        """
        stats["source_chars"] += stats_obj.all_chars
        stats["source_words"] += stats_obj.all_words
        stats["source_strings"] += stats_obj.all

    def prefetch_source(self):
        """Fetches source statistics data."""
        return

    def _calculate_basic(self):
        stats = zero_stats(self.basic_keys)
        for obj in self.object_set:
            stats_obj = obj.stats
            # Aggregate non source_* values
            for item in BASIC_KEYS:
                aggregate(stats, item, stats_obj)
            # source_* values have specific implementations
            self.calculate_source(stats_obj, stats)
        for category in self.category_set:
            stats_obj = category.stats
            # This does full aggegation as categories have proper source_* values
            for item in self.basic_keys:
                aggregate(stats, item, stats_obj)

        for key, value in stats.items():
            self.store(key, value)

        # If source_* was not iterated before, it needs to be handled now
        with sentry_sdk.start_span(op="stats", description=f"SOURCE {self.cache_key}"):
            self.prefetch_source()


class SingleLanguageStats(AggregatingStats):
    def _calculate_basic(self):
        super()._calculate_basic()
        self.store("languages", 1)

    def get_single_language_stats(self, language):
        return self

    @cached_property
    def is_source(self):
        return self.obj.is_source


class ParentAggregatingStats(AggregatingStats):
    def calculate_source(self, stats_obj, stats):
        aggregate(stats, "source_chars", stats_obj)
        aggregate(stats, "source_words", stats_obj)
        aggregate(stats, "source_strings", stats_obj)


class LanguageStats(AggregatingStats):
    @cached_property
    def object_set(self):
        return prefetch_stats(self._object.translation_set.only("id", "language"))


class ComponentStats(AggregatingStats):
    @cached_property
    def object_set(self):
        return prefetch_stats(self._object.translation_set.only("id", "component"))

    @cached_property
    def has_review(self):
        return self._object.enable_review

    def calculate_source(self, stats_obj, stats):
        return

    def prefetch_source(self):
        source_translation = self._object.get_source_translation()
        if source_translation is None:
            self.store("source_chars", 0)
            self.store("source_words", 0)
            self.store("source_strings", 0)
        else:
            stats_obj = source_translation.stats
            self.store("source_chars", stats_obj.all_chars)
            self.store("source_words", stats_obj.all_words)
            self.store("source_strings", stats_obj.all)

    def get_update_objects(self):
        yield self._object.project.stats
        yield from self._object.project.stats.get_update_objects()

        if self._object.category:
            yield self._object.category.stats
            yield from self._object.category.stats.get_update_objects()

        for clist in self._object.componentlist_set.all():
            yield clist.stats
            yield from clist.stats.get_update_objects()

        yield from super().get_update_objects()

    def update_language_stats(self):
        extras = []

        # Update languages
        for translation in self.object_set:
            translation.stats.update_stats(update_parents=False)
            extras.extend(translation.stats.get_update_objects())

        # Update our stats
        self.update_stats()

        # Update all parents
        self.update_parents(extras)

    def get_language_stats(self):
        yield from (TranslationStats(translation) for translation in self.object_set)

    def get_single_language_stats(self, language):
        try:
            return TranslationStats(self._object.translation_set.get(language=language))
        except ObjectDoesNotExist:
            return DummyTranslationStats(language)


class ProjectLanguageComponent:
    is_glossary = False

    def __init__(self, parent):
        self.slug = "-"
        self.parent = parent

    @property
    def translation_set(self):
        return self.parent.translation_set

    @property
    def context_label(self):
        return self.translation_set[0].component.context_label

    @property
    def source_language(self):
        return self.translation_set[0].component.source_language


class ProjectLanguage(BaseURLMixin):
    """Wrapper class used in project-language listings and stats."""

    remove_permission = "translation.delete"
    settings_permission = "project.edit"

    def __init__(self, project, language: Language):
        self.project = project
        self.language = language
        self.component = ProjectLanguageComponent(self)

    def __str__(self):
        return f"{self.project} - {self.language}"

    @property
    def code(self):
        return self.language.code

    @cached_property
    def stats(self):
        return ProjectLanguageStats(self)

    def get_share_url(self):
        """Return absolute URL usable for sharing."""
        return get_site_url(
            reverse(
                "engage",
                kwargs={"path": self.get_url_path()},
            )
        )

    def get_widgets_url(self):
        """Return absolute URL for widgets."""
        return f"{self.project.get_widgets_url()}?lang={self.language.code}"

    @cached_property
    def pk(self):
        return f"{self.project.pk}-{self.language.pk}"

    @cached_property
    def cache_key(self):
        return f"{self.project.cache_key}-{self.language.pk}"

    def get_url_path(self):
        return [*self.project.get_url_path(), "-", self.language.code]

    def get_absolute_url(self):
        return reverse("show", kwargs={"path": self.get_url_path()})

    def get_translate_url(self):
        return reverse("translate", kwargs={"path": self.get_url_path()})

    @cached_property
    def translation_set(self):
        all_langs = self.language.translation_set.prefetch()
        result = all_langs.filter(component__project=self.project).union(
            all_langs.filter(component__links=self.project)
        )
        for item in result:
            item.is_shared = (
                None
                if item.component.project == self.project
                else item.component.project
            )
        return sorted(
            result,
            key=lambda trans: (trans.component.priority, trans.component.name.lower()),
        )

    @cached_property
    def is_source(self):
        return self.language.id in self.project.source_language_ids

    @cached_property
    def change_set(self):
        return self.project.change_set.filter(language=self.language)

    @cached_property
    def workflow_settings(self):
        from weblate.trans.models.workflow import WorkflowSetting

        workflow_settings = WorkflowSetting.objects.filter(
            Q(project=None) | Q(project=self.project),
            language=self.language,
        )
        if len(workflow_settings) == 0:
            return None
        if len(workflow_settings) == 1:
            return workflow_settings[0]
        # We should have two objects here, return project specific one
        for workflow_setting in workflow_settings:
            if workflow_setting.project_id == self.project.id:
                return workflow_setting
        raise WorkflowSetting.DoesNotExist


class ProjectLanguageStats(SingleLanguageStats):
    def __init__(self, obj: ProjectLanguage, project_stats=None):
        self.language = obj.language
        self.project = obj.project
        self._project_stats = project_stats
        super().__init__(obj)
        obj.stats = self

    @cached_property
    def has_review(self):
        return self.project.source_review or self.project.translation_review

    @cached_property
    def category_set(self):
        if self._project_stats:
            return self._project_stats.category_set
        return prefetch_stats(self.project.category_set.only("id", "project"))

    @cached_property
    def object_set(self):
        return prefetch_stats(
            self.language.translation_set.filter(component__project=self.project).only(
                "id", "language"
            )
        )


class CategoryLanguage(BaseURLMixin):
    """Wrapper class used in category-language listings and stats."""

    remove_permission = "translation.delete"

    def __init__(self, category, language: Language):
        self.category = category
        self.language = language
        self.component = ProjectLanguageComponent(self)

    def __str__(self):
        return f"{self.category} - {self.language}"

    @property
    def code(self):
        return self.language.code

    @cached_property
    def stats(self):
        return CategoryLanguageStats(self)

    @cached_property
    def pk(self):
        return f"{self.category.pk}-{self.language.pk}"

    @cached_property
    def cache_key(self):
        return f"{self.category.cache_key}-{self.language.pk}"

    def get_url_path(self):
        return [*self.category.get_url_path(), "-", self.language.code]

    def get_absolute_url(self):
        return reverse("show", kwargs={"path": self.get_url_path()})

    def get_translate_url(self):
        return reverse("translate", kwargs={"path": self.get_url_path()})

    @cached_property
    def translation_set(self):
        result = self.language.translation_set.filter(
            component__category=self.category
        ).prefetch()
        for item in result:
            item.is_shared = (
                None
                if item.component.project == self.category.project
                else item.component.project
            )
        return sorted(
            result,
            key=lambda trans: (trans.component.priority, trans.component.name.lower()),
        )

    @cached_property
    def is_source(self):
        return self.language.id in self.category.source_language_ids

    @cached_property
    def change_set(self):
        return self.language.change_set.for_category(self.category)


class CategoryLanguageStats(SingleLanguageStats):
    def __init__(self, obj: CategoryLanguage, category_stats=None):
        self.language = obj.language
        self.category = obj.category
        self._category_stats = category_stats
        super().__init__(obj)
        obj.stats = self

    @cached_property
    def has_review(self):
        return (
            self.category.project.source_review
            or self.category.project.translation_review
        )

    @cached_property
    def category_set(self):
        if self._category_stats:
            return self._category_stats.category_set
        return prefetch_stats(self.category.category_set.only("id", "category"))

    @cached_property
    def object_set(self):
        return prefetch_stats(
            self.language.translation_set.filter(
                component__category=self.category
            ).only("id", "language")
        )


class CategoryStats(ParentAggregatingStats):
    def get_update_objects(self):
        yield self._object.project.stats
        yield from self._object.project.stats.get_update_objects()

        if self._object.category:
            yield self._object.category.stats
            yield from self._object.category.stats.get_update_objects()

        yield from super().get_update_objects()

    @cached_property
    def object_set(self):
        return prefetch_stats(
            self._object.component_set.only("id", "category").prefetch_source_stats()
        )

    @cached_property
    def category_set(self):
        return prefetch_stats(self._object.category_set.only("id", "category"))

    def get_single_language_stats(self, language):
        return CategoryLanguageStats(
            CategoryLanguage(self._object, language), category_stats=self
        )

    def get_language_stats(self):
        result = [
            self.get_single_language_stats(language)
            for language in self._object.languages
        ]
        return prefetch_stats(result)


class ProjectStats(ParentAggregatingStats):
    @cached_property
    def has_review(self):
        return self._object.enable_review

    @cached_property
    def category_set(self):
        return prefetch_stats(
            self._object.category_set.filter(category=None).only("id", "project")
        )

    @cached_property
    def object_set(self):
        return prefetch_stats(
            self._object.component_set.only("id", "project").prefetch_source_stats()
        )

    def get_single_language_stats(self, language):
        return ProjectLanguageStats(
            ProjectLanguage(self._object, language), project_stats=self
        )

    def get_language_stats(self):
        result = [
            self.get_single_language_stats(language)
            for language in self._object.languages
        ]
        return prefetch_stats(result)

    def _calculate_basic(self):
        super()._calculate_basic()
        self.store("languages", self._object.languages.count())


class ComponentListStats(ParentAggregatingStats):
    @cached_property
    def object_set(self):
        return prefetch_stats(
            self._object.components.only("id", "componentlist").prefetch_source_stats()
        )


class GlobalStats(ParentAggregatingStats):
    def __init__(self):
        super().__init__(None)

    @cached_property
    def object_set(self):
        from weblate.trans.models import Project

        return prefetch_stats(Project.objects.only("id", "access_control"))

    def _calculate_basic(self):
        super()._calculate_basic()
        self.store("languages", Language.objects.have_translation().count())

    @cached_property
    def cache_key(self):
        return "stats-global"


class GhostStats(BaseStats):
    basic_keys = SOURCE_KEYS
    is_ghost = True

    def __init__(self, base=None):
        super().__init__(None)
        self.base = base

    @cached_property
    def pk(self):
        return get_random_identifier()

    def _calculate_basic(self):
        stats = zero_stats(self.basic_keys)
        if self.base is not None:
            for key in "all", "all_words", "all_chars":
                stats[key] = getattr(self.base, key)
            stats["todo"] = stats["all"]
            stats["todo_words"] = stats["all_words"]
            stats["todo_chars"] = stats["all_chars"]
        for key, value in stats.items():
            self.store(key, value)

    @cached_property
    def cache_key(self):
        return "stats-zero"

    def save(self, update_parents: bool = True):
        return

    def load(self):
        return {}

    def get_absolute_url(self):
        return None


class GhostProjectLanguageStats(GhostStats):
    def __init__(self, component, language, is_shared=None):
        super().__init__(component.stats)
        self.language = language
        self.component = component
        self.is_shared = is_shared
