#!/usr/bin/env node
'use strict';

/**
 * Calculadora CLI interativa em Node.js
 * Operações: soma, subtração, multiplicação, divisão,
 *            potência e raiz quadrada.
 * Usa o módulo readline nativo do Node.js.
 */

const readline = require('readline');

// ──────────────────────────────────────────────
// Funções de operação
// ──────────────────────────────────────────────

function soma(a, b) {
  return a + b;
}

function subtracao(a, b) {
  return a - b;
}

function multiplicacao(a, b) {
  return a * b;
}

function divisao(a, b) {
  if (b === 0) {
    throw new Error('Divisão por zero não é permitida.');
  }
  return a / b;
}

function potencia(base, expoente) {
  return Math.pow(base, expoente);
}

function raizQuadrada(valor) {
  if (valor < 0) {
    throw new Error('Não existe raiz quadrada real de número negativo.');
  }
  return Math.sqrt(valor);
}

// ──────────────────────────────────────────────
// Utilitários de entrada
// ──────────────────────────────────────────────

/**
 * Pergunta ao usuário e retorna uma Promise com a resposta.
 */
function perguntar(rl, texto) {
  return new Promise((resolve) => {
    rl.question(texto, (resposta) => resolve(resposta.trim()));
  });
}

/**
 * Solicita um número válido ao usuário.
 * Repete até receber um valor numérico válido.
 */
async function lerNumero(rl, prompt) {
  while (true) {
    const entrada = await perguntar(rl, prompt);
    const valor = Number(entrada);
    if (entrada !== '' && !Number.isNaN(valor) && Number.isFinite(valor)) {
      return valor;
    }
    console.log('  ⚠ Entrada inválida. Digite um número válido.');
  }
}

// ──────────────────────────────────────────────
// Menu
// ──────────────────────────────────────────────

function exibirMenu() {
  console.log('\n════════════════════════════════════════');
  console.log('          CALCULADORA CLI');
  console.log('════════════════════════════════════════');
  console.log('  1) Soma              (a + b)');
  console.log('  2) Subtração         (a - b)');
  console.log('  3) Multiplicação     (a * b)');
  console.log('  4) Divisão           (a / b)');
  console.log('  5) Potência          (a ^ b)');
  console.log('  6) Raiz Quadrada     (√a)');
  console.log('  0) Sair');
  console.log('════════════════════════════════════════\n');
}

// ──────────────────────────────────────────────
// Execução de operações
// ──────────────────────────────────────────────

async function executarOperacao(rl, opcao) {
  let a, b, resultado, simbolo;

  try {
    switch (opcao) {
      case '1':
        a = await lerNumero(rl, 'Digite o primeiro valor (a): ');
        b = await lerNumero(rl, 'Digite o segundo valor  (b): ');
        resultado = soma(a, b);
        simbolo = '+';
        break;

      case '2':
        a = await lerNumero(rl, 'Digite o primeiro valor (a): ');
        b = await lerNumero(rl, 'Digite o segundo valor  (b): ');
        resultado = subtracao(a, b);
        simbolo = '-';
        break;

      case '3':
        a = await lerNumero(rl, 'Digite o primeiro valor (a): ');
        b = await lerNumero(rl, 'Digite o segundo valor  (b): ');
        resultado = multiplicacao(a, b);
        simbolo = '*';
        break;

      case '4':
        a = await lerNumero(rl, 'Digite o primeiro valor (a): ');
        b = await lerNumero(rl, 'Digite o segundo valor  (b): ');
        resultado = divisao(a, b);
        simbolo = '/';
        break;

      case '5':
        a = await lerNumero(rl, 'Digite a base: ');
        b = await lerNumero(rl, 'Digite o expoente: ');
        resultado = potencia(a, b);
        simbolo = '^';
        break;

      case '6':
        a = await lerNumero(rl, 'Digite o valor: ');
        resultado = raizQuadrada(a);
        simbolo = '√';
        break;

      default:
        console.log('  ⚠ Opção inválida. Escolha uma opção de 0 a 6.');
        return;
    }

    // Exibe resultado
    if (opcao === '6') {
      console.log(`\n  ✓ √${a} = ${resultado}`);
    } else {
      console.log(`\n  ✓ ${a} ${simbolo} ${b} = ${resultado}`);
    }
  } catch (erro) {
    console.log(`\n  ✗ Erro: ${erro.message}`);
  }
}

// ──────────────────────────────────────────────
// Loop principal
// ──────────────────────────────────────────────

async function loopPrincipal() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });

  console.log('\nBem-vindo à Calculadora CLI! Use Ctrl+C para sair a qualquer momento.');

  while (true) {
    exibirMenu();
    const opcao = await perguntar(rl, 'Escolha uma opção: ');

    if (opcao === '0' || opcao.toLowerCase() === 'sair') {
      console.log('\nAté logo! 👋\n');
      rl.close();
      break;
    }

    await executarOperacao(rl, opcao);

    // Pausa antes de voltar ao menu
    await perguntar(rl, '\nPressione ENTER para continuar...');
  }
}

// ──────────────────────────────────────────────
// Ponto de entrada
// ──────────────────────────────────────────────

loopPrincipal().catch((erro) => {
  console.error('Erro inesperado:', erro);
  process.exit(1);
});