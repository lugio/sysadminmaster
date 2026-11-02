/**
 * table-utils.js — Design System SysAdminMaster
 * Filtre + tri génériques pour toute table. Règle du design system :
 * une table sans filtre/tri n'est pas conforme, sauf exception explicite
 * (data-no-enhance) documentée en commentaire dans le template.
 *
 * Usage :
 *   <input data-filter-for="ma-table" class="form-control" placeholder="Filtrer...">
 *   <table id="ma-table" data-enhance>
 *     <thead><tr><th data-sort="text">Hôte</th><th data-sort="num">Sessions</th></tr></thead>
 *     ...
 *   </table>
 * Zéro dépendance, pas de framework.
 */
(function () {
    function detectType(th) {
        return th.dataset.sort || 'text';
    }

    function cellValue(tr, colIndex) {
        const cell = tr.children[colIndex];
        if (!cell) return '';
        // data-sort-value prime sur le texte affiché — nécessaire quand l'affichage
        // formate la valeur (ex. "2.1 GB" / "512.3 MB" : un tri sur le texte brut
        // comparerait "2.1" à "512.3" sans tenir compte de l'unité).
        if (cell.dataset.sortValue !== undefined) return cell.dataset.sortValue;
        return cell.textContent.trim();
    }

    function sortTable(table, colIndex, type, dir) {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const rows = Array.from(tbody.rows);
        rows.sort((a, b) => {
            let va = cellValue(a, colIndex), vb = cellValue(b, colIndex);
            if (type === 'num') {
                va = parseFloat(va.replace(',', '.').replace(/[^0-9.\-]/g, '')) || 0;
                vb = parseFloat(vb.replace(',', '.').replace(/[^0-9.\-]/g, '')) || 0;
                return dir === 'asc' ? va - vb : vb - va;
            }
            return dir === 'asc' ? va.localeCompare(vb, 'fr') : vb.localeCompare(va, 'fr');
        });
        rows.forEach((r) => tbody.appendChild(r));
    }

    function enhanceSort(table) {
        const ths = table.querySelectorAll('thead th');
        ths.forEach((th, colIndex) => {
            if (th.dataset.noSort !== undefined) return;
            const type = detectType(th);
            th.classList.add('th-sortable');
            th.dataset.sortDir = '';
            const arrow = document.createElement('span');
            arrow.className = 'sort-arrow material-symbols-rounded';
            arrow.textContent = 'unfold_more';
            th.appendChild(arrow);

            th.addEventListener('click', () => {
                const nextDir = th.dataset.sortDir === 'asc' ? 'desc' : 'asc';
                ths.forEach((other) => {
                    other.dataset.sortDir = '';
                    const a = other.querySelector('.sort-arrow');
                    if (a) a.textContent = 'unfold_more';
                    other.classList.remove('th-sorted');
                });
                th.dataset.sortDir = nextDir;
                th.classList.add('th-sorted');
                arrow.textContent = nextDir === 'asc' ? 'arrow_upward' : 'arrow_downward';
                sortTable(table, colIndex, type, nextDir);
            });
        });
    }

    function enhanceFilter(table) {
        if (!table.id) return;
        const input = document.querySelector(`[data-filter-for="${table.id}"]`);
        if (!input) return;
        input.addEventListener('input', () => {
            const q = input.value.trim().toLowerCase();
            const body = table.tBodies[0];
            const rows = body ? Array.from(body.rows).filter((r) => !r.dataset.noMatch) : [];
            let visibles = 0;
            rows.forEach((r) => {
                const ok = !q || r.textContent.toLowerCase().includes(q);
                r.style.display = ok ? '' : 'none';
                if (ok) visibles += 1;
            });

            // 🔴 Un tableau qui se vide SANS RIEN DIRE se lit « il n'y a rien »,
            // pas « votre filtre n'a rien trouvé ». Constaté le 01/09/2026 sur
            // /licenses : un nom de personne saisi dans le filtre des SERVEURS a
            // vidé le tableau, et la page a semblé cassée. On l'annonce, et on
            // indique le bon outil quand il y en a un (data-filter-hint).
            if (!body) return;
            let ligne = body.querySelector('tr[data-no-match]');
            if (q && visibles === 0) {
                if (!ligne) {
                    ligne = body.insertRow();
                    ligne.dataset.noMatch = '1';
                    const td = ligne.insertCell();
                    td.colSpan = (table.tHead && table.tHead.rows[0]) ? table.tHead.rows[0].cells.length : 1;
                    td.className = 'text-subtle text-center';
                    td.style.padding = '18px';
                }
                const hint = input.dataset.filterHint ? ' ' + input.dataset.filterHint : '';
                ligne.cells[0].textContent = `Aucune ligne ne correspond à « ${input.value.trim()} ».` + hint;
                ligne.style.display = '';
            } else if (ligne) {
                ligne.style.display = 'none';
            }
        });
    }

    function init() {
        document.querySelectorAll('table[data-enhance]').forEach((table) => {
            enhanceSort(table);
            enhanceFilter(table);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
