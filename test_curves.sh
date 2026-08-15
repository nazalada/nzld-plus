#!/bin/bash
# Проверка чистой математики из ve_hook.cpp: bez_at (безье-выборка фокуса), merge_times
# (объединение сеток времён) и layer_corners (наклон плоскости слоя).
# Функции ВЫРЕЗАЮТСЯ из ve_hook.cpp, а не копируются — иначе тест бы разъехался с кодом
# и молча проверял вчерашнюю версию.
# Запуск: ./test_curves.sh
set -e
cd "$(dirname "$0")"
T=$(mktemp -d)
{
  echo '#include <stdio.h>'
  echo '#include <math.h>'
  echo '#define RIG_MAXK 80'
  echo '#define RIG_MAXL 8'
  echo '#define RIG_SIGN_Y -1.0'
  echo 'struct KfEase { int cubic; double vti, vi, vto, vo; };'
  echo 'struct Scene { int dummy; };'
  echo 'static struct { int keepin; int nlayer; struct { double z,bx,by,base_scale,base_rot,tiltx,tilty; } L[RIG_MAXL]; } g_rig;'
  sed -n '/^static double bez_at/,/^}$/p' ve_hook.cpp
  sed -n '/^static int merge_times/,/^}$/p' ve_hook.cpp
  sed -n '/^static void cp_grow_to_frame/,/^}$/p' ve_hook.cpp
  sed -n '/^static void layer_corners/,/^}$/p' ve_hook.cpp
  cat <<'EOF'
#define OK(c) do{ if(!(c)){ printf("ПРОВАЛ: %s (строка %d)\n", #c, __LINE__); bad++; } }while(0)
int main(){ int bad=0;
  double ts[3]={0,100,200}, vs[3]={0,1,0};
  KfEase lin[3]={{0,0,0,0,0},{0,0,0,0,0},{0,0,0,0,0}};
  OK(fabs(bez_at(ts,vs,lin,3,50)-0.5)<1e-9);      // без кривых = линейная интерполяция
  OK(fabs(bez_at(ts,vs,lin,3,150)-0.5)<1e-9);
  OK(bez_at(ts,vs,lin,3,-10)==0.0);               // до первого ключа — держим первое значение
  OK(bez_at(ts,vs,lin,3,999)==0.0);               // после последнего — держим последнее
  OK(bez_at(ts,vs,0,3,50)==0.5);                  // es==0 не должен падать
  OK(bez_at(ts,vs,lin,0,50)==0.0);                // пустая кривая
  // Ручки — АБСОЛЮТНЫЕ смещения от ключа (x в µs, y в единицах свойства), НЕ доли интервала.
  // Здесь dt=100, dv=1, поэтому ease-in-out 0.42/0.58 записывается как vto=42, vti=-42.
  KfEase eio[3]={{1,0,0,42,0.0},{1,-42,0.0,42,0.0},{1,-42,0.0,0,0}};
  OK(fabs(bez_at(ts,vs,eio,3,50)-0.5)<1e-6);      // симметричный ease: середина остаётся серединой
  OK(bez_at(ts,vs,eio,3,25)<0.25);                // ease-in: в начале отстаём от линейного
  double prev=-1; for(int t=0;t<=100;t+=5){ double v=bez_at(ts,vs,eio,3,t); OK(v>=prev-1e-9); prev=v; }
  // РЕГРЕСС на единицы ручек. Числа — живые, из draft_info.json проекта «3D CAMERA», свойство
  // KFTypeScaleX: dt=500000, dv=+0.50727846, right_control={440000, 0.071018984}. Нормировка даёт
  // пресет 0.88/0.14, он точечно-симметричен ⇒ середина интервала обязана быть серединой значения.
  // Тест ловит именно старую ошибку: при чтении ручек как долей 440000 клампилось в 1, а vi меняло
  // знак, и кривая уезжала. dt и dv здесь РАЗНЫЕ и не равны 1 — на долях сойтись не может.
  double rt[2]={0,500000}, rv[2]={1.0, 1.0+0.507278460};
  KfEase rk[2]={{1,0,0,440000,0.071018984},{1,-440000,-0.071018984,0,0}};
  OK(fabs(bez_at(rt,rv,rk,2,250000) - (1.0+0.507278460/2)) < 1e-6);
  OK(bez_at(rt,rv,rk,2,125000) < 1.0+0.507278460*0.25);   // 0.88/0.14 держит начало пологим
  double a[3]={0,100,200}, b[3]={50,100,150}, out[16];
  const double *src[2]={a,b}; const int cnt[2]={3,3};
  OK(merge_times(src,cnt,2,out,16)==5);           // слияние с дедупом
  double want[5]={0,50,100,150,200};
  for(int i=0;i<5;i++) OK(out[i]==want[i]);       // и по возрастанию
  const int c1[2]={3,0};
  OK(merge_times(src,c1,2,out,16)==3);            // пустой источник не ломает
  OK(merge_times(src,cnt,2,out,3)==3);            // переполнение обрезается, а не пишет за край

  // --- наклон плоскости слоя ---
  double q[8], f=1080*0.8, hw=960, hh=540;
  g_rig.nlayer=1; g_rig.L[0].z=2.0; g_rig.L[0].bx=0; g_rig.L[0].by=0;
  g_rig.L[0].base_scale=1.0; g_rig.L[0].base_rot=0; g_rig.L[0].tiltx=0; g_rig.L[0].tilty=0;
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
  OK(fabs(q[0]+1)<1e-9 && fabs(q[2]-1)<1e-9);     // без наклона и камеры — ровно рамка кадра
  OK(fabs(q[1]+1)<1e-9 && fabs(q[5]-1)<1e-9);   // Y ВНИЗ: верхние углы -1, нижние +1
  layer_corners(0,0, 30,10,0.5, 0, f,hw,hh, q, 0,0);
  OK(fabs((q[2]-q[0])-(q[6]-q[4]))<1e-9);         // без наклона остаётся биллбордом даже при движении камеры
  g_rig.L[0].tilty=35;
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
  OK(fabs(fabs(q[1]-q[5])-fabs(q[3]-q[7]))>1e-3); // поворот вокруг вертикали: края разной высоты
  OK(fabs(q[0]-q[4])<1e-9 && fabs(q[2]-q[6])<1e-9);
  g_rig.L[0].tilty=0; g_rig.L[0].tiltx=35;
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
  OK(fabs((q[2]-q[0])-(q[6]-q[4]))>1e-3);         // поворот вокруг горизонтали: верх и низ разной ширины
  OK(fabs(q[1]-q[3])<1e-9 && fabs(q[5]-q[7])<1e-9);
  double wtop=q[2]-q[0];
  g_rig.L[0].tiltx=-35; layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
  OK(fabs((q[6]-q[4])-wtop)<1e-9);                // знак наклона зеркалит картинку
  g_rig.L[0].tiltx=89.9; layer_corners(0,0, 0,0,1.99, 0, f,hw,hh, q, 0,0);
  for(int i=0;i<8;i++) OK(isfinite(q[i]));        // угол за камерой не даёт NaN/inf
  // нормировка площади: наклон меняет форму, но НЕ раздувает слой (иначе на 40° он вылезал вдвое)
  for(double a=5;a<=55;a+=10){
    g_rig.L[0].tiltx=0; g_rig.L[0].tilty=a;
    layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
    const int o[4]={0,1,3,2}; double ar=0;
    for(int k=0;k<4;k++){ int i=o[k], j=o[(k+1)&3]; ar+=q[i*2]*q[j*2+1]-q[j*2]*q[i*2+1]; }
    ar=fabs(ar)*0.5;
    OK(fabs(ar-4.0)<1e-6);                        // площадь как у нетронутой рамки (2x2)
    OK(fabs(fabs(q[1]-q[5])-fabs(q[3]-q[7]))>1e-4); // но перспектива на месте: края разной высоты
  }

  // --- ПОВОРОТ КАМЕРЫ (эмуляция 3D) ---
  g_rig.L[0].tiltx=0; g_rig.L[0].tilty=0; g_rig.keepin=0;
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
  double flatw=q[2]-q[0];
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 25,0);      // рыскание вправо
  OK(fabs(fabs(q[1]-q[5])-fabs(q[3]-q[7]))>1e-3);       // края слоя разной высоты = перспектива
  OK(q[0]<0 && q[2]<flatw);                             // сцена уехала влево (камера смотрит вправо)
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, -25,0);
  OK(q[0]>-1);                                          // обратный поворот — в другую сторону
  layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,25);       // тангаж
  OK(fabs(fabs(q[2]-q[0])-fabs(q[6]-q[4]))>1e-3);       // верх и низ разной ширины
  { // ОБЛЁТ вокруг точки: слой НА её глубине почти стоит, ближний и дальний расходятся
    // в РАЗНЫЕ стороны — это и есть признак объёма, а не плоского панорамирования.
    // Центр меряем на КРОШЕЧНОМ слое: у большого углы лежат на разной глубине, и их среднее
    // из-за перспективы центру не равно.
    double zp=2.6, th=20*M_PI/180.0;
    double ccx=-zp*sin(th), ccz=zp*(1-cos(th));
    double qn[8], qp[8], qf[8];
    double bs0=g_rig.L[0].base_scale; g_rig.L[0].base_scale=0.002;
    g_rig.L[0].z=1.2; layer_corners(0,0, ccx,0,ccz, 0, f,hw,hh, qn, 20,0);
    g_rig.L[0].z=zp;  layer_corners(0,0, ccx,0,ccz, 0, f,hw,hh, qp, 20,0);
    g_rig.L[0].z=4.0; layer_corners(0,0, ccx,0,ccz, 0, f,hw,hh, qf, 20,0);
    double cn=(qn[0]+qn[2])*0.5, cp=(qp[0]+qp[2])*0.5, cf=(qf[0]+qf[2])*0.5;
    OK(fabs(cp)<0.01);          // слой на точке облёта остаётся по центру
    OK(cn*cf<0);                // ближний и дальний ушли в РАЗНЫЕ стороны
    OK(fabs(cn)>0.05 && fabs(cf)>0.05);
    g_rig.L[0].base_scale=bs0; g_rig.L[0].z=2.0; }
  for(double a=-40;a<=40;a+=10){                        // без NaN на всём диапазоне
    layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, a, a*0.6);
    for(int i=0;i<8;i++) OK(isfinite(q[i])); }
  g_rig.keepin=1;                                       // рамка закрывается и при повороте
  for(double a=-40;a<=40;a+=20){
    layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, a, a*0.5);
    const int o2[4]={0,1,3,2};
    for(int c=0;c<4;c++){
      double px=(c&1)?1.0:-1.0, py=(c&2)?1.0:-1.0; int inside=1, sgn=0;
      for(int e=0;e<4;e++){ int i=o2[e], j=o2[(e+1)&3];
        double cr=(q[j*2]-q[i*2])*(py-q[i*2+1])-(q[j*2+1]-q[i*2+1])*(px-q[i*2]);
        int sg=(cr>1e-9)-(cr<-1e-9); if(!sg) continue;
        if(!sgn) sgn=sg; else if(sg!=sgn) inside=0; }
      OK(inside); } }
  g_rig.keepin=0;

  // «не выходить за рамку» при наклоне: трапеция обязана накрыть все четыре угла кадра
  g_rig.keepin=1;
  for(double a=-50;a<=50;a+=12.5){
    g_rig.L[0].tiltx=a*0.4; g_rig.L[0].tilty=a;
    layer_corners(0,0, 0,0,0, 0, f,hw,hh, q, 0,0);
    const int o[4]={0,1,3,2};
    for(int c=0;c<4;c++){                       // угол кадра внутри трапеции?
      double px=(c&1)?1.0:-1.0, py=(c&2)?1.0:-1.0; int inside=1, sgn=0;
      for(int e=0;e<4;e++){ int i=o[e], j=o[(e+1)&3];
        double cr=(q[j*2]-q[i*2])*(py-q[i*2+1])-(q[j*2+1]-q[i*2+1])*(px-q[i*2]);
        int s=(cr>1e-9)-(cr<-1e-9); if(!s) continue;
        if(!sgn) sgn=s; else if(s!=sgn) inside=0; }
      OK(inside);
    }
  }
  g_rig.keepin=0;

  printf(bad?"ТЕСТ ПРОВАЛЕН: %d\n":"все проверки прошли\n", bad);
  return bad;
}
EOF
} > "$T/t.cpp"
clang++ -std=c++17 -O0 -o "$T/t" "$T/t.cpp"
"$T/t"
