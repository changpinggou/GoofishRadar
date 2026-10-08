"""T03 单元测试：配置加载/校验/硬顶截断/字段级报错/keyword 文件操作。"""
from __future__ import annotations

import yaml
import pytest

from goofishradar.config import (
    HARD_MAX_PAGES_PER_DAY,
    HARD_MAX_PAGES_PER_KEYWORD,
    ConfigError,
    enabled_tasks,
    keyword_add,
    keyword_remove,
    load_keywords,
    load_settings,
)

SETTINGS_KEYS = ("schedule", "limits", "ranking", "report", "paths")


def write_yaml(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def default_settings_data() -> dict:
    return {
        "schedule": {"time": "08:00", "randomize_minutes": 15},
        "limits": {"page_delay_seconds": [8, 20], "max_pages_per_keyword": 3,
                   "max_pages_per_day": 300},
        "ranking": {"topn_overall": 50, "topn_per_keyword": 20, "topn_rising": 20,
                    "weights": {"want_count": 0.40, "view_count": 0.20,
                                "want_delta_7d": 0.25, "view_delta_7d": 0.15}},
        "report": {"retain_days": 90},
    }


def default_keywords_data() -> dict:
    return {"tasks": [{"keyword": "iPhone", "enabled": True, "max_pages": 3,
                       "sort": "most_want"}]}


# ---- settings.yaml ----

class TestLoadSettings:
    def test_missing_file_falls_back_to_defaults_with_warning(self, tmp_path):
        settings, warnings = load_settings(tmp_path / "none.yaml")
        assert any("默认值" in w for w in warnings)
        assert settings.limits.max_pages_per_day == 300
        assert settings.limits.page_delay_seconds == (8.0, 20.0)
        assert settings.schedule.time == "08:00"

    def test_yaml_edit_takes_effect_without_code_change(self, tmp_path):
        data = default_settings_data()
        data["ranking"]["topn_overall"] = 77
        data["limits"]["page_delay_seconds"] = [10, 25]
        data["schedule"]["weekend_time"] = "09:30"
        path = tmp_path / "settings.yaml"
        write_yaml(path, data)

        settings, warnings = load_settings(path)
        assert warnings == []
        assert settings.ranking.topn_overall == 77
        assert settings.limits.page_delay_seconds == (10.0, 25.0)
        assert settings.schedule.resolve_time(weekend=True) == "09:30"
        assert settings.schedule.resolve_time(weekend=False) == "08:00"

    def test_hard_cap_truncates_pages_and_warns(self, tmp_path):
        data = default_settings_data()
        data["limits"]["max_pages_per_keyword"] = 100
        data["limits"]["max_pages_per_day"] = 999
        settings, warnings = load_settings(write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml")
        assert settings.limits.max_pages_per_keyword == HARD_MAX_PAGES_PER_KEYWORD == 50
        assert settings.limits.max_pages_per_day == HARD_MAX_PAGES_PER_DAY == 300
        joined = "\n".join(warnings)
        assert "已自动截断" in joined
        assert joined.count("已自动截断") == 2

    def test_type_error_names_exact_field(self, tmp_path):
        data = default_settings_data()
        data["limits"]["max_pages_per_keyword"] = "three"
        path = write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml"
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "limits.max_pages_per_keyword" in str(ei.value)

    def test_bool_is_not_int(self, tmp_path):
        data = default_settings_data()
        data["limits"]["max_pages_per_day"] = True
        path = write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml"
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "limits.max_pages_per_day" in str(ei.value)

    def test_bad_time_format_and_range(self, tmp_path):
        data = default_settings_data()
        data["schedule"]["time"] = "25:00"
        path = write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml"
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "schedule.time" in str(ei.value)

    def test_weights_must_sum_to_one(self, tmp_path):
        data = default_settings_data()
        data["ranking"]["weights"]["want_count"] = 0.9
        path = write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml"
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "ranking.weights" in str(ei.value) and "1.0" in str(ei.value)

    def test_delay_reversed_is_error_too_fast_is_warning(self, tmp_path):
        data = default_settings_data()
        data["limits"]["page_delay_seconds"] = [20, 8]
        path = write_yaml(tmp_path / "s.yaml", data) or tmp_path / "s.yaml"
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "page_delay_seconds" in str(ei.value)

        data["limits"]["page_delay_seconds"] = [2, 4]
        settings, warnings = load_settings(write_yaml(tmp_path / "s2.yaml", data)
                                           or tmp_path / "s2.yaml")
        assert any("风控" in w for w in warnings)

    def test_unknown_key_warns(self, tmp_path):
        data = default_settings_data()
        data["limits"]["max_page"] = 3  # 拼写错误
        settings, warnings = load_settings(write_yaml(tmp_path / "s.yaml", data)
                                           or tmp_path / "s.yaml")
        assert any("limits.max_page" in w and "未知" in w for w in warnings)

    def test_yaml_syntax_error_is_config_error(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("limits: [1, 2", encoding="utf-8")
        with pytest.raises(ConfigError) as ei:
            load_settings(path)
        assert "语法错误" in str(ei.value)

    def test_paths_section_resolves_relative_and_creates_dirs(self, tmp_path, monkeypatch):
        from goofishradar import config as cfg_mod, paths as paths_mod
        monkeypatch.setattr(cfg_mod, "ROOT", tmp_path)
        data = default_settings_data()
        data["paths"] = {"data_dir": "mydata", "reports_dir": "myreports",
                         "logs_dir": "mylogs"}
        settings, _ = load_settings(write_yaml(tmp_path / "s.yaml", data)
                                    or tmp_path / "s.yaml")
        assert settings.paths.data_dir == tmp_path / "mydata"
        assert settings.paths.user_data_dir == tmp_path / "mydata" / "user_data"
        assert (tmp_path / "mydata" / "user_data").exists()
        assert (tmp_path / "myreports").exists()


# ---- keywords.yaml ----

class TestLoadKeywords:
    def load(self, tmp_path, tasks, name="k.yaml"):
        path = tmp_path / name
        write_yaml(path, {"tasks": tasks})
        return load_keywords(path)

    def test_missing_file_is_error(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            load_keywords(tmp_path / "none.yaml")
        assert "不存在" in str(ei.value)

    def test_empty_tasks_is_error(self, tmp_path):
        with pytest.raises(ConfigError):
            self.load(tmp_path, [])

    def test_missing_keyword_names_field(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            self.load(tmp_path, [{"enabled": True}])
        assert "tasks[0].keyword" in str(ei.value)

    def test_bad_sort_lists_allowed_values(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            self.load(tmp_path, [{"keyword": "x", "sort": "hot"}])
        assert "most_want" in str(ei.value)

    def test_task_pages_truncated_with_warn(self, tmp_path):
        tasks, warnings = self.load(
            tmp_path, [{"keyword": "x", "max_pages": 80}])
        assert tasks[0].max_pages == HARD_MAX_PAGES_PER_KEYWORD
        assert any("已自动截断" in w for w in warnings)

    def test_price_cross_is_error(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            self.load(tmp_path, [{"keyword": "x", "price_min": 500, "price_max": 100}])
        assert "price_min" in str(ei.value)

    def test_duplicate_keyword_warns_and_dedupes(self, tmp_path):
        tasks, warnings = self.load(
            tmp_path, [{"keyword": "x"}, {"keyword": "x"}])
        assert len(tasks) == 1
        assert any("重复" in w for w in warnings)

    def test_all_disabled_warns_and_enabled_tasks_filters(self, tmp_path):
        tasks, warnings = self.load(
            tmp_path, [{"keyword": "a", "enabled": False},
                       {"keyword": "b", "enabled": False}])
        assert enabled_tasks(tasks) == []
        assert any("停用" in w for w in warnings)

    def test_full_optional_fields(self, tmp_path):
        tasks, warnings = self.load(
            tmp_path, [{"keyword": "x", "enabled": True, "max_pages": 2,
                        "sort": "newest", "price_min": 100, "price_max": 900,
                        "stuff": "九成新", "location": "上海"}])
        assert warnings == []
        t = tasks[0]
        assert (t.sort, t.price_min, t.price_max, t.stuff, t.location) == \
            ("newest", 100.0, 900.0, "九成新", "上海")


# ---- keyword 增删文件操作 ----

class TestKeywordFileOps:
    def make(self, tmp_path):
        path = tmp_path / "keywords.yaml"
        write_yaml(path, default_keywords_data())
        return path

    def test_add_then_reload_roundtrip(self, tmp_path):
        path = self.make(tmp_path)
        keyword_add(path, "乐高", max_pages=5, sort="newest",
                    price_min=50, price_max=3000, location="北京")
        tasks, warnings = load_keywords(path)
        assert warnings == []
        assert [t.keyword for t in tasks] == ["iPhone", "乐高"]
        added = tasks[1]
        assert (added.max_pages, added.sort, added.price_min, added.price_max,
                added.location) == (5, "newest", 50.0, 3000.0, "北京")

    def test_add_duplicate_is_error(self, tmp_path):
        path = self.make(tmp_path)
        with pytest.raises(ConfigError):
            keyword_add(path, "iPhone")

    def test_add_invalid_sort_is_error_before_write(self, tmp_path):
        path = self.make(tmp_path)
        with pytest.raises(ConfigError):
            keyword_add(path, "x", sort="hot")
        tasks, _ = load_keywords(path)
        assert [t.keyword for t in tasks] == ["iPhone"]  # 文件未被破坏

    def test_remove_and_missing(self, tmp_path):
        path = tmp_path / "keywords.yaml"
        write_yaml(path, {"tasks": [{"keyword": "a"}, {"keyword": "b"}]})
        assert keyword_remove(path, "a") is True
        tasks, _ = load_keywords(path)
        assert [t.keyword for t in tasks] == ["b"]
        assert keyword_remove(path, "不存在的词") is False  # 不存在 → False，文件不动
        with pytest.raises(ConfigError):  # 最后一个任务不允许移除
            keyword_remove(path, "b")
        tasks, _ = load_keywords(path)
        assert [t.keyword for t in tasks] == ["b"]

    def test_cannot_remove_last_task(self, tmp_path):
        path = self.make(tmp_path)
        with pytest.raises(ConfigError):
            keyword_remove(path, "iPhone")  # 仅剩 0 个 → 拒绝


# ---- 仓库自带配置必须始终合法（防止提交坏配置）----

class TestRepoConfigFiles:
    def test_repo_settings_and_keywords_are_valid(self):
        from goofishradar.paths import CONFIG_DIR
        settings, s_warn = load_settings(CONFIG_DIR / "settings.yaml")
        tasks, k_warn = load_keywords(CONFIG_DIR / "keywords.yaml")
        assert settings.limits.max_pages_per_keyword <= HARD_MAX_PAGES_PER_KEYWORD
        assert len(enabled_tasks(tasks)) >= 1
        assert s_warn == [], s_warn
        assert k_warn == [], k_warn
