#!/usr/bin/env python3
"""
Calculadora CLI interativa
Operações: soma, subtração, multiplicação, divisão, potência e raiz quadrada.
"""

import math
import sys


# ---------------------------------------------------------------------------
# Funções de operação
# ---------------------------------------------------------------------------

def somar(a: float, b: float) -> float:
    """Retorna a soma de a e b."""
    return a + b


def subtrair(a: float, b: float) -> float:
    """Retorna a subtração de a por b."""
    return a - b


def multiplicar(a: float, b: float) -> float:
    """Retorna a multiplicação de a por b."""
    return a * b


def dividir(a: float, b: float) -> float:
    """Retorna a divisão de a por b. Levanta ValueError se b for zero."""
    if b == 0:
        raise ValueError("Divisão por zero não é permitida.")
    return a / b


def potenciar(base: float, expoente: float) -> float:
    """Retorna base elevada ao expoente."""
    return base ** expoente


def raiz_quadrada(valor: float) -> float:
    """Retorna a raiz quadrada de valor. Levanta ValueError se valor for negativo."""
    if valor < 0:
        raise ValueError("Raiz quadrada de número negativo não é permitida (números reais).")
    return math.sqrt(valor)


# ---------------------------------------------------------------------------
# Utilidades de entrada
# ---------------------------------------------------------------------------

def ler_numero(prompt: str) -> float:
    """
    Lê um número float do stdin.
    Repete até que o usuário digite um valor válido.
    """
    while True:
        try:
            return float(input(prompt))
        except ValueError:
            print("  ⚠ Entrada inválida. Digite um número válido (ex.: 3.14).")


def exibir_menu() -> None:
    """Imprime o menu de operações."""
    print("\n" + "=" * 40)
    print("       CALCULADORA EM PYTHON")
    print("=" * 40)
    print("  1) Somar            (a + b)")
    print("  2) Subtrair         (a - b)")
    print("  3) Multiplicar      (a * b)")
    print("  4) Dividir          (a / b)")
    print("  5) Potência         (a ^ b)")
    print("  6) Raiz quadrada    (√a)")
    print("  0) Sair")
    print("=" * 40)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def main() -> None:
    """Loop principal da calculadora CLI."""
    while True:
        exibir_menu()
        escolha = input("Escolha uma opção: ").strip()

        # --- Sair ---------------------------------------------------------
        if escolha == "0":
            print("Encerrando a calculadora. Até logo!")
            break

        # --- Operações binárias -------------------------------------------
        if escolha in ("1", "2", "3", "4", "5"):
            a = ler_numero("  Digite o primeiro número (a): ")
            b = ler_numero("  Digite o segundo número (b): ")

            try:
                if escolha == "1":
                    resultado = somar(a, b)
                    operacao = f"{a} + {b}"
                elif escolha == "2":
                    resultado = subtrair(a, b)
                    operacao = f"{a} - {b}"
                elif escolha == "3":
                    resultado = multiplicar(a, b)
                    operacao = f"{a} * {b}"
                elif escolha == "4":
                    resultado = dividir(a, b)
                    operacao = f"{a} / {b}"
                elif escolha == "5":
                    resultado = potenciar(a, b)
                    operacao = f"{a} ^ {b}"

                print(f"\n  ✅ Resultado: {operacao} = {resultado}")

            except ValueError as e:
                print(f"\n  ❌ Erro: {e}")

        # --- Raiz quadrada (unária) --------------------------------------
        elif escolha == "6":
            a = ler_numero("  Digite o número (a): ")
            try:
                resultado = raiz_quadrada(a)
                print(f"\n  ✅ Resultado: √{a} = {resultado}")
            except ValueError as e:
                print(f"\n  ❌ Erro: {e}")

        # --- Opção inválida ----------------------------------------------
        else:
            print("\n  ⚠ Opção inválida. Escolha um número entre 0 e 6.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCalculadora interrompida pelo usuário. Até logo!")
        sys.exit(0)