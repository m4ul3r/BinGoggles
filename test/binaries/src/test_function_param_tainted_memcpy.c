#include <string.h>
#include <stdio.h>

void test_shared_object_four(void *arg1, void *arg2, void *arg3) {
    /*
        memcpy test case
    */
   printf("dummy %s\n", arg2);
   memcpy(arg1, arg3, 100);
}

int main() {
    char buf_one[100];
    char buf_two[100];
    char buf_three[50] = {0};
    memcpy(buf_three, "hello world", 11);

    printf("buf one: ");
    fgets(buf_one, 100, stdin);

    printf("buf two: ");
    fgets(buf_two, 100, stdin);

    test_shared_object_four(buf_one, buf_three, buf_two);
    return 0;

}