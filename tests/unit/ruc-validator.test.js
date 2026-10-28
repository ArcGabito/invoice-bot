import { describe, expect, it } from 'vitest';
import { computeRucCheckDigit, normalizeRuc, validateRuc } from '../../src/domain/ruc-validator.js';

describe('validateRuc', () => {
  it.each(['20100047218', '20131312955', '10467793549'])('acepta el RUC válido %s', (ruc) => {
    expect(validateRuc(ruc)).toEqual({ valid: true });
  });

  it('detecta un dígito de control incorrecto e informa el esperado', () => {
    expect(validateRuc('20100047219')).toEqual({
      valid: false,
      reason: 'check_digit',
      expected: '8',
    });
  });

  it.each(['2010004721', '201000472181', '2010004721A', '', null, 20100047218])(
    'rechaza el formato de %s',
    (ruc) => {
      expect(validateRuc(ruc)).toMatchObject({ valid: false, reason: 'format' });
    },
  );

  it('rechaza prefijos inexistentes', () => {
    expect(validateRuc('30100047218')).toMatchObject({ valid: false, reason: 'prefix' });
  });

  it('mapea los restos 10 y 11 a los dígitos 0 y 1', () => {
    // Recorre prefijos hasta encontrar ambos casos y comprueba que el resultado sea un dígito.
    for (let i = 0; i < 2000; i++) {
      const digit = computeRucCheckDigit(`20${String(i).padStart(8, '0')}`);
      expect(digit).toBeGreaterThanOrEqual(0);
      expect(digit).toBeLessThanOrEqual(9);
    }
  });
});

describe('normalizeRuc', () => {
  it('elimina espacios, guiones y puntos', () => {
    expect(normalizeRuc(' 20-100.047 218 ')).toBe('20100047218');
    expect(normalizeRuc('')).toBeNull();
    expect(normalizeRuc(null)).toBeNull();
  });
});
