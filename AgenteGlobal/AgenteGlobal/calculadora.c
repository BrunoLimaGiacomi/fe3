/*
 * calculadora.c
 *
 * Calculadora interativa em C (CLI) com menu textual.
 * Operacoes suportadas:
 *   1) Soma
 *   2) Subtracao
 *   3) Multiplicacao
 *   4) Divisao (com tratamento de divisao por zero)
 *   5) Potencia (base^expoente)
 *   6) Raiz quadrada (com tratamento de numero negativo)
 *   0) Sair
 *
 * Compilar:  gcc calculadora.c -o calculadora -lm
 * Executar:  ./calculadora
 */

#include <stdio.h>
#include <stdlib.h>
#include <math.h>

/* ------------------------------------------------------------------ */
/*  Funcoes de operacao                                               */
/* ------------------------------------------------------------------ */

double somar(double a, double b)
{
    return a + b;
}

double subtrair(double a, double b)
{
    return a - b;
}

double multiplicar(double a, double b)
{
    return a * b;
}

/*
 * Retorna o resultado da divisao via ponteiro "resultado".
 * Retorna 0 em sucesso, -1 se o divisor for zero.
 */
int dividir(double a, double b, double *resultado)
{
    if (b == 0.0) {
        return -1;          /* divisao por zero */
    }
    *resultado = a / b;
    return 0;
}

double potencia(double base, double expoente)
{
    return pow(base, expoente);
}

/*
 * Retorna a raiz quadrada via ponteiro "resultado".
 * Retorna 0 em sucesso, -1 se o argumento for negativo.
 */
int raiz_quadrada(double x, double *resultado)
{
    if (x < 0.0) {
        return -1;          /* raiz de numero negativo */
    }
    *resultado = sqrt(x);
    return 0;
}

/* ------------------------------------------------------------------ */
/*  Utilitarios de entrada                                            */
/* ------------------------------------------------------------------ */

/*
 * Le um double do stdin com tratamento de entrada invalida.
 * Descarta caracteres restantes do buffer para evitar loops infinitos.
 * Retorna 0 em sucesso, -1 em EOF.
 */
int ler_double(const char *prompt, double *valor)
{
    int ret;

    while (1) {
        printf("%s", prompt);
        fflush(stdout);

        ret = scanf("%lf", valor);
        if (ret == EOF) {
            return -1;
        }
        if (ret == 1) {
            /* limpa restante da linha */
            int c;
            while ((c = getchar()) != '\n' && c != EOF)
                ;
            return 0;
        }

        /* entrada invalida: limpa buffer e tenta de novo */
        printf("Entrada invalida. Tente novamente.\n");
        int c;
        while ((c = getchar()) != '\n' && c != EOF)
            ;
    }
}

/*
 * Le um int do stdin com tratamento de entrada invalida.
 * Retorna 0 em sucesso, -1 em EOF.
 */
int ler_int(const char *prompt, int *valor)
{
    int ret;

    while (1) {
        printf("%s", prompt);
        fflush(stdout);

        ret = scanf("%d", valor);
        if (ret == EOF) {
            return -1;
        }
        if (ret == 1) {
            int c;
            while ((c = getchar()) != '\n' && c != EOF)
                ;
            return 0;
        }

        printf("Entrada invalida. Tente novamente.\n");
        int c;
        while ((c = getchar()) != '\n' && c != EOF)
            ;
    }
}

/* ------------------------------------------------------------------ */
/*  Programa principal                                                */
/* ------------------------------------------------------------------ */

int main(void)
{
    int opcao;
    double a, b, resultado;

    printf("========================================\n");
    printf("        Calculadora em C (CLI)          \n");
    printf("========================================\n");

    while (1) {
        printf("\n");
        printf("----- Menu de Operacoes -----\n");
        printf("  1) Soma            (a + b)\n");
        printf("  2) Subtracao       (a - b)\n");
        printf("  3) Multiplicacao   (a * b)\n");
        printf("  4) Divisao         (a / b)\n");
        printf("  5) Potencia        (a ^ b)\n");
        printf("  6) Raiz quadrada   (sqrt(a))\n");
        printf("  0) Sair\n");
        printf("------------------------------\n");

        if (ler_int("Escolha uma opcao: ", &opcao) == -1) {
            printf("\nEOF detectado. Saindo...\n");
            break;
        }

        switch (opcao) {

        case 0:
            printf("Encerrando a calculadora. Ate logo!\n");
            return 0;

        /* ---- Operacoes binarias ---- */

        case 1:
            if (ler_double("Digite o primeiro valor (a): ", &a) == -1) goto eof_exit;
            if (ler_double("Digite o segundo valor  (b): ", &b) == -1) goto eof_exit;
            resultado = somar(a, b);
            printf("Resultado: %.6g + %.6g = %.6g\n", a, b, resultado);
            break;

        case 2:
            if (ler_double("Digite o primeiro valor (a): ", &a) == -1) goto eof_exit;
            if (ler_double("Digite o segundo valor  (b): ", &b) == -1) goto eof_exit;
            resultado = subtrair(a, b);
            printf("Resultado: %.6g - %.6g = %.6g\n", a, b, resultado);
            break;

        case 3:
            if (ler_double("Digite o primeiro valor (a): ", &a) == -1) goto eof_exit;
            if (ler_double("Digite o segundo valor  (b): ", &b) == -1) goto eof_exit;
            resultado = multiplicar(a, b);
            printf("Resultado: %.6g * %.6g = %.6g\n", a, b, resultado);
            break;

        case 4:
            if (ler_double("Digite o primeiro valor (a): ", &a) == -1) goto eof_exit;
            if (ler_double("Digite o segundo valor  (b): ", &b) == -1) goto eof_exit;
            if (dividir(a, b, &resultado) != 0) {
                printf("Erro: divisao por zero nao e permitida.\n");
            } else {
                printf("Resultado: %.6g / %.6g = %.6g\n", a, b, resultado);
            }
            break;

        case 5:
            if (ler_double("Digite a base        (a): ", &a) == -1) goto eof_exit;
            if (ler_double("Digite o expoente    (b): ", &b) == -1) goto eof_exit;
            resultado = potencia(a, b);
            printf("Resultado: %.6g ^ %.6g = %.6g\n", a, b, resultado);
            break;

        /* ---- Operacao unaria ---- */

        case 6:
            if (ler_double("Digite o valor (a): ", &a) == -1) goto eof_exit;
            if (raiz_quadrada(a, &resultado) != 0) {
                printf("Erro: nao existe raiz quadrada real de numero negativo.\n");
            } else {
                printf("Resultado: sqrt(%.6g) = %.6g\n", a, resultado);
            }
            break;

        /* ---- Opcao invalida ---- */

        default:
            printf("Opcao invalida. Escolha um numero entre 0 e 6.\n");
            break;
        }
    }

eof_exit:
    printf("\nEOF detectado. Saindo...\n");
    return 0;
}