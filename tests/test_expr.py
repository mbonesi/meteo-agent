import pytest

from meteoagent.expr import Expression, ExprError, dir_between, dir_in, sector


class TestSector:
    @pytest.mark.parametrize(
        "degrees,expected",
        [
            (0, "N"), (360, "N"), (359, "N"), (11, "N"),
            (12, "NNE"), (45, "NE"), (67.5, "ENE"), (90, "E"),
            (112.5, "ESE"), (135, "SE"), (180, "S"), (270, "W"),
            (720 + 45, "NE"),  # normalization beyond a full turn
        ],
    )
    def test_boundaries(self, degrees, expected):
        assert sector(degrees) == expected

    def test_sector_width_is_22_5_centred(self):
        # the NE sector (45 degrees) covers [33.75, 56.25)
        assert sector(33.8) == "NE"
        assert sector(56.2) == "NE"
        assert sector(33.7) == "NNE"
        assert sector(56.3) == "ENE"


class TestDirIn:
    def test_east_quadrant(self):
        quadrant = ["NE", "ENE", "E", "ESE", "SE"]
        assert dir_in(100, quadrant)
        assert dir_in(45, quadrant)
        assert dir_in(135, quadrant)
        assert not dir_in(0, quadrant)
        assert not dir_in(200, quadrant)
        assert not dir_in(30, quadrant)  # NNE, just outside

    def test_accepts_italian_notation(self):
        assert dir_in(270, ["O"])  # O = Ovest = W

    def test_unknown_sector_raises(self):
        with pytest.raises(ExprError, match="unknown sector"):
            dir_in(100, ["XX"])


class TestDirBetween:
    def test_simple_arc(self):
        assert dir_between(90, 45, 135)
        assert not dir_between(200, 45, 135)

    def test_wraps_around_north(self):
        assert dir_between(350, 340, 20)
        assert dir_between(10, 340, 20)
        assert not dir_between(180, 340, 20)


class TestExpression:
    def test_simple_comparison(self):
        e = Expression("wind_speed_10m > 20")
        assert e.evaluate({"wind_speed_10m": 25})
        assert not e.evaluate({"wind_speed_10m": 15})

    def test_names_excludes_functions(self):
        e = Expression('dir_in(wind_direction_10m, ["E"]) and wind_speed_10m > 20')
        assert e.names == {"wind_direction_10m", "wind_speed_10m"}

    def test_the_real_rule(self):
        e = Expression('dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45')
        assert e.evaluate({"wind_direction_10m": 100, "wind_gusts_10m": 50})
        assert not e.evaluate({"wind_direction_10m": 100, "wind_gusts_10m": 40})
        assert not e.evaluate({"wind_direction_10m": 280, "wind_gusts_10m": 50})

    def test_chained_comparison(self):
        e = Expression("0 < temperature_2m < 10")
        assert e.evaluate({"temperature_2m": 5})
        assert not e.evaluate({"temperature_2m": 15})

    def test_or_and_not(self):
        e = Expression("not (a > 1) or b == 2")
        assert e.evaluate({"a": 0, "b": 0})
        assert e.evaluate({"a": 5, "b": 2})
        assert not e.evaluate({"a": 5, "b": 0})

    def test_arithmetic(self):
        assert Expression("a * 2 + 1 > 5").evaluate({"a": 3})

    def test_missing_variable_raises(self):
        with pytest.raises(ExprError, match="undefined variables"):
            Expression("a > 1").evaluate({})


class TestSafety:
    """The expression comes from a configuration file: it must not be able to execute anything."""

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('rm -rf /')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__",
            "a.b",
            "a[0]",
            "lambda: 1",
            "[x for x in range(3)]",
            "a if b else c",
            "eval('1')",
            "exec('x=1')",
            "print('hello')",
            "a := 1",
            "f'{a}'",
        ],
    )
    def test_rejects_dangerous_constructs(self, source):
        with pytest.raises(ExprError):
            Expression(source)

    def test_rejects_syntax_error(self):
        with pytest.raises(ExprError, match="syntax"):
            Expression("a >")

    def test_rejects_empty(self):
        with pytest.raises(ExprError, match="empty"):
            Expression("   ")

    def test_rejects_unknown_function(self):
        with pytest.raises(ExprError, match="unknown function"):
            Expression("pippo(1)")

    def test_rejects_keyword_arguments(self):
        with pytest.raises(ExprError, match="keyword"):
            Expression("round(a, ndigits=1)")
