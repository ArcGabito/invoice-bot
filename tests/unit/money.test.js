import { describe, expect, it } from 'vitest';
import {
  applyRateBasisPoints,
  formatCents,
  normalizeDecimalString,
  toCents,
} from '../../src/shared/money.js';

describe('toCents', () => {
  it.each([
    ['118.00', 11800],
    ['118', 11800],
    ['0.5', 50],
    ['1,234.56', 123456],
    ['1.234,56', 123456],
    ['1.234.567', 123456700],
    ['12,5', 1250],
    ['S/ 1,180.00', 118000],
    ['US$ 99.99', 9999],
    ['(10.00)', -1000],
    [118.0, 11800],
    [1.005, 101], // redondeo comercial sobre la representación decimal, no sobre el binario
    ['2.675', 268],
  ])('%s → %i', (input, expected) => {
    expect(toCents(input)).toBe(expected);
  });

  it('devuelve null para valores vacíos', () => {
    expect(toCents(null)).toBeNull();
    expect(toCents('')).toBeNull();
    expect(toCents('  ')).toBeNull();
  });

  it('rechaza texto no numérico', () => {
    expect(() => toCents('ciento dieciocho')).toThrow(TypeError);
    expect(() => normalizeDecimalString('12.3.4,5,6')).toThrow(TypeError);
  });

  it('no arrastra errores de coma flotante', () => {
    expect(toCents(0.1) + toCents(0.2)).toBe(30);
  });
});

describe('formatCents', () => {
  it('formatea con dos decimales y signo', () => {
    expect(formatCents(11800)).toBe('118.00');
    expect(formatCents(5)).toBe('0.05');
    expect(formatCents(-1050)).toBe('-10.50');
    expect(formatCents(null)).toBe('');
  });
});

describe('applyRateBasisPoints', () => {
  it('calcula el 18 % con redondeo mitad hacia arriba', () => {
    expect(applyRateBasisPoints(10000, 1800)).toBe(1800);
    expect(applyRateBasisPoints(8475, 1800)).toBe(1526); // 15.255 → 15.26
    expect(applyRateBasisPoints(10000, 1050)).toBe(1050);
  });
});
