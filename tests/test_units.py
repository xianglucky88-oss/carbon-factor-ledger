import unittest

from carbon_factor_ledger.units import (
    UnitError,
    convert_quantity,
    dimension_of,
    emission_to_kgco2e,
    parse_factor_unit,
)


class TestUnits(unittest.TestCase):
    def test_mass_conversion_chain(self):
        # t -> kg -> g across the mass dimension
        self.assertEqual(convert_quantity(2, "t", "kg"), 2000.0)
        self.assertEqual(convert_quantity(5000, "g", "kg"), 5.0)
        self.assertAlmostEqual(convert_quantity(10, "lb", "kg"), 4.5359237)

    def test_energy_and_distance(self):
        self.assertEqual(convert_quantity(1, "MWh", "kWh"), 1000.0)
        self.assertAlmostEqual(convert_quantity(3.6, "GJ", "kWh"), 1000.0)
        self.assertEqual(convert_quantity(1, "km", "m"), 1000.0)

    def test_freight_unit_tkm(self):
        # kgkm is 1/1000 of a tkm
        self.assertEqual(convert_quantity(10000, "kgkm", "tkm"), 10.0)

    def test_same_unit_is_exact(self):
        self.assertEqual(convert_quantity(7.5, "kg", "kg"), 7.5)

    def test_cross_dimension_is_rejected_not_zeroed(self):
        with self.assertRaises(UnitError):
            convert_quantity(50, "kWh", "kg")
        with self.assertRaises(UnitError):
            convert_quantity(50, "kg", "tkm")
        self.assertEqual(dimension_of("kWh"), "energy")
        self.assertEqual(dimension_of("tkm"), "mass_distance")

    def test_unknown_unit(self):
        with self.assertRaises(UnitError):
            convert_quantity(1, "frobnicate", "kg")

    def test_factor_unit_parsing_and_emission_scale(self):
        num, ref = parse_factor_unit("kgCO2e/tkm")
        self.assertEqual((num, ref), ("kgCO2e", "tkm"))
        with self.assertRaises(UnitError):
            parse_factor_unit("watts/kg")
        # tCO2e expressed back in kgCO2e
        self.assertEqual(emission_to_kgco2e(1.0, "tCO2e"), 1000.0)
        self.assertEqual(emission_to_kgco2e(1000.0, "gCO2e"), 1.0)


if __name__ == "__main__":
    unittest.main()
