import { afterAll } from 'vitest';
import { closeWorkerDatabase } from './helpers/db.ts';

afterAll(closeWorkerDatabase);
