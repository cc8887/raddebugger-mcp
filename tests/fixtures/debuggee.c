#include <windows.h>
#include <stdio.h>
#include <string.h>

volatile int g_counter = 7;

__declspec(noinline) int breakpoint_here(int input)
{
    volatile int local_value = input + 5;
    g_counter = local_value; /* MCP_LINE_BREAKPOINT */
    return local_value * 2;
}

int main(int argc, char **argv)
{
    char auth_probe[2];
    if(GetEnvironmentVariableA("RAD_MCP_TOKEN", auth_probe, sizeof(auth_probe)) != 0) { return 98; }
    if(argc > 1 && strcmp(argv[1], "--wait-for-debugger") == 0)
    {
        while(!IsDebuggerPresent()) { Sleep(10); }
        Sleep(200);
    }
    if(argc > 1 && strcmp(argv[1], "--loop") == 0)
    {
        for(;;) { g_counter += 1; Sleep(20); }
    }
    int answer = breakpoint_here(7);
    answer = breakpoint_here(7); /* MCP_SECOND_CALL */
    printf("answer=%d counter=%d\n", answer, g_counter);
    return answer == 24 && g_counter == 12 ? 0 : 1;
}
