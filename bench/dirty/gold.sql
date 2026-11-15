-- Gold SQL for bench/dirty/questions.yaml. Rules: shipped = stat '20'; cancelled = '90'; open = '10'; del_flg '1' rows are excluded everywhere;
-- corporate = kbn '2'; bank transfer = hoho_kbn '2'; t_juchu.kokyaku_cd -> m_kokyaku.kokyaku_id (no FK).
-- Q1
select count(*) from m_kokyaku where kbn='2';
-- Q2
select sum(m.suryo*m.tanka) from t_juchu j join t_juchu_mei m on m.juchu_no=j.juchu_no where j.stat='20' and j.del_flg='0' and j.juchu_dt between '2026-01-01' and '2026-12-31';
-- Q3
select sum(m.suryo*m.tanka) from t_juchu j join t_juchu_mei m on m.juchu_no=j.juchu_no join m_kokyaku k on k.kokyaku_id=j.kokyaku_cd where k.kbn='2' and j.stat='20' and j.del_flg='0' and j.juchu_dt between '2026-01-01' and '2026-12-31';
-- Q4
select k.kokyaku_id from t_juchu j join t_juchu_mei m on m.juchu_no=j.juchu_no join m_kokyaku k on k.kokyaku_id=j.kokyaku_cd where j.stat='20' and j.del_flg='0' and j.juchu_dt between '2026-01-01' and '2026-12-31' group by k.kokyaku_id order by sum(m.suryo*m.tanka) desc limit 1;
-- Q5
select count(*) from t_juchu where stat='90' and del_flg='0' and juchu_dt between '2026-01-01' and '2026-12-31';
-- Q6
select sum(n.kingaku) from t_nyukin n join t_juchu j on j.juchu_no=n.juchu_no where n.hoho_kbn='2' and j.del_flg='0' and n.nyukin_dt between '2026-01-01' and '2026-12-31';
-- Q7
select j.tanto_cd from t_juchu j join t_juchu_mei m on m.juchu_no=j.juchu_no where j.stat='20' and j.del_flg='0' and j.juchu_dt between '2026-01-01' and '2026-12-31' group by j.tanto_cd order by sum(m.suryo*m.tanka) desc limit 1;
-- Q8
select count(*) from t_juchu where stat='10' and del_flg='0';
-- Q9
select (select coalesce(sum(m.suryo*m.tanka),0) from t_juchu j join t_juchu_mei m on m.juchu_no=j.juchu_no where j.kokyaku_cd=103 and j.stat='20' and j.del_flg='0')
     - (select coalesce(sum(n.kingaku),0) from t_nyukin n join t_juchu j on j.juchu_no=n.juchu_no where j.kokyaku_cd=103 and j.stat='20' and j.del_flg='0');
-- Q10
select count(*) from t_juchu_mei m join m_shohin s on s.shohin_cd=m.shohin_cd join t_juchu j on j.juchu_no=m.juchu_no where m.tanka < s.tanka_std and j.del_flg='0';
