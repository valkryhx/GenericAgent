#!/usr/bin/env node
'use strict';

const { parse } = require('acorn');

const DEFAULT_MAX_BYTES = 524288;
const RESERVED_META_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const PARSE_ERROR_HINT =
  'Workflow scripts must be plain JavaScript \u2014 common causes are TypeScript syntax (type annotations, interfaces, generics) and broken string quoting or escaping.';

function formatParseError(error, source) {
  const message = error instanceof Error ? error.message : String(error);
  const loc = error && typeof error === 'object' ? error.loc : undefined;
  if (
    !loc ||
    typeof loc.line !== 'number' ||
    typeof loc.column !== 'number'
  ) {
    return `Script parse error: ${message}. ${PARSE_ERROR_HINT}`;
  }
  const lines = source.split('\n');
  const line = lines[loc.line - 1];
  if (line === undefined) {
    return `Script parse error: ${message}. ${PARSE_ERROR_HINT}`;
  }
  const column = Math.max(0, Math.min(loc.column, line.length));
  const windowSize = 80;
  const start = Math.max(0, Math.min(column - Math.floor(windowSize / 2), line.length - windowSize));
  const snippet = line.slice(start, start + windowSize);
  const caret = `${' '.repeat(column - start)}^`;
  return `Script parse error: ${message}\n\n${snippet}\n${caret}\n\n${PARSE_ERROR_HINT}`;
}

function isMetaExportStatement(statement) {
  if (!statement || statement.type !== 'ExportNamedDeclaration') return false;
  const declaration = statement.declaration;
  if (!declaration || declaration.type !== 'VariableDeclaration') return false;
  if (declaration.kind !== 'const' || declaration.declarations.length !== 1) return false;
  const declarator = declaration.declarations[0];
  return Boolean(
    declarator &&
      declarator.id &&
      declarator.id.type === 'Identifier' &&
      declarator.id.name === 'meta' &&
      declarator.init &&
      declarator.init.type === 'ObjectExpression',
  );
}

function metaKeyName(property) {
  let key;
  if (property.key.type === 'Identifier') key = property.key.name;
  else if (property.key.type === 'Literal') key = String(property.key.value);
  else throw new Error(`unsupported key type in meta: ${property.key.type}`);
  if (RESERVED_META_KEYS.has(key)) {
    throw new Error(`reserved key name not allowed in meta: ${key}`);
  }
  return key;
}

function evaluateMetaObject(node) {
  const result = Object.create(null);
  for (const property of node.properties) {
    if (property.type !== 'Property') {
      throw new Error('only plain properties allowed in meta');
    }
    if (property.computed) throw new Error('computed keys not allowed in meta');
    if (property.method || property.kind !== 'init') {
      throw new Error('methods/accessors not allowed in meta');
    }
    result[metaKeyName(property)] = evaluateMetaLiteral(property.value);
  }
  return result;
}

function evaluateMetaLiteral(node) {
  switch (node.type) {
    case 'Literal':
      return node.value;
    case 'ArrayExpression':
      return node.elements.map((element) => {
        if (element === null) throw new Error('sparse arrays not allowed');
        if (element.type === 'SpreadElement') throw new Error('spread not allowed in meta');
        return evaluateMetaLiteral(element);
      });
    case 'ObjectExpression':
      return evaluateMetaObject(node);
    case 'TemplateLiteral': {
      if (node.expressions.length > 0) {
        throw new Error('template interpolation not allowed in meta');
      }
      return node.quasis.map((quasi) => quasi.value.cooked ?? '').join('');
    }
    case 'UnaryExpression':
      if (
        node.operator === '-' &&
        node.argument.type === 'Literal' &&
        typeof node.argument.value === 'number'
      ) {
        return -node.argument.value;
      }
      throw new Error('only negative-number unary allowed in meta');
    default:
      throw new Error(`non-literal node type in meta: ${node.type}`);
  }
}

function normalizePhases(value) {
  if (!Array.isArray(value)) return undefined;
  const phases = [];
  for (const phase of value) {
    if (!phase || typeof phase !== 'object' || !('title' in phase)) continue;
    const { title, detail, model } = phase;
    if (typeof title === 'string') {
      phases.push({
        title,
        detail: typeof detail === 'string' ? detail : undefined,
        model: typeof model === 'string' ? model : undefined,
      });
    }
  }
  return phases.length > 0 ? phases : undefined;
}

function validateMeta(value) {
  const name = value && value.name;
  if (typeof name !== 'string' || name.length === 0) {
    return { error: 'meta.name must be a non-empty string' };
  }
  const description = value && value.description;
  if (typeof description !== 'string' || description.length === 0) {
    return { error: 'meta.description must be a non-empty string' };
  }
  const title = typeof value.title === 'string' && value.title.length > 0 ? value.title : undefined;
  const whenToUse = typeof value.whenToUse === 'string' ? value.whenToUse : undefined;
  const phases = normalizePhases(value.phases);
  return { meta: { name, description, title, whenToUse, phases } };
}

function extractScriptBody(source, statementEnd) {
  return source
    .slice(statementEnd)
    .replace(/^[;\s]*\n/, '')
    .trimStart();
}

function parseWorkflowScript(script, options = {}) {
  const source = String(script ?? '');
  const maxBytes = options.maxBytes === undefined ? DEFAULT_MAX_BYTES : Number(options.maxBytes);
  if (source.length > maxBytes) return { error: `Script exceeds ${maxBytes} bytes` };

  let ast;
  try {
    ast = parse(source, {
      ecmaVersion: 'latest',
      sourceType: 'module',
      allowAwaitOutsideFunction: true,
      allowReturnOutsideFunction: true,
    });
  } catch (error) {
    return { error: formatParseError(error, source) };
  }

  const first = ast.body[0];
  if (!isMetaExportStatement(first)) {
    return {
      error: '`export const meta = { name, description, phases }` must be the FIRST statement in the script',
    };
  }

  let value;
  try {
    value = evaluateMetaLiteral(first.declaration.declarations[0].init);
  } catch (error) {
    return {
      error: `meta must be a pure literal: ${error instanceof Error ? error.message : String(error)}`,
    };
  }

  const validated = validateMeta(value);
  if ('error' in validated) return validated;
  return {
    meta: validated.meta,
    scriptBody: extractScriptBody(source, first.end),
  };
}

function main() {
  let source = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => {
    source += chunk;
  });
  process.stdin.on('end', () => {
    const result = parseWorkflowScript(source);
    if ('error' in result) {
      process.stdout.write(`${JSON.stringify({ ok: false, error: result.error })}\n`);
      process.exitCode = 1;
      return;
    }
    process.stdout.write(`${JSON.stringify({ ok: true, meta: result.meta, scriptBody: result.scriptBody })}\n`);
  });
}

if (require.main === module) main();

module.exports = {
  DEFAULT_MAX_BYTES,
  RESERVED_META_KEYS,
  formatParseError,
  parseWorkflowScript,
};
